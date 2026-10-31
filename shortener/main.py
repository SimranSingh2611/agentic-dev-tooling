import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl, field_validator
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from shortener.models import Base, Click, Link, RateBucket, make_engine, session_factory


class LinkCreate(BaseModel):
    url: HttpUrl
    # FEATURE_REQUEST: workflow scenarios extend the request here.

    @field_validator("url")
    @classmethod
    def validate_destination(cls, url):
        if len(str(url)) > 2048 or url.username or url.password:
            raise ValueError("URLs must be at most 2048 characters and contain no credentials")
        return url


class LinkView(BaseModel):
    code: str
    url: str
    short_url: str
    created_at: datetime


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def create_app(database_url: str | None = None, api_key: str | None = None, rate_limit: int = 60):
    database_url = database_url or os.getenv(
        "SHORTENER_DATABASE_URL", "sqlite:///data/shortener.db"
    )
    api_key = api_key or os.getenv("SHORTENER_API_KEY", "local-shortener-key")
    engine = make_engine(database_url)
    sessions = session_factory(engine)

    @asynccontextmanager
    async def lifespan(_app):
        # Initial schema bootstrap only; subsequent changes require explicit migrations.
        Base.metadata.create_all(engine)
        yield
        engine.dispose()

    app = FastAPI(title="URL Shortener", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        started = time.monotonic()
        request_id = secrets.token_hex(12)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        route = request.scope.get("route")
        logging.getLogger("uvicorn.error").info(
            json.dumps(
                {
                    "event": "http.request",
                    "request_id": request_id,
                    "method": request.method,
                    "route": getattr(route, "path", "unmatched"),
                    "status": response.status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            )
        )
        return response

    def authenticated(x_api_key: Annotated[str | None, Header()] = None):
        if not x_api_key or not hmac.compare_digest(x_api_key, api_key):
            raise HTTPException(401, "A valid X-API-Key is required")

    def limited(request: Request):
        minute = int(time.time() // 60)
        client = request.client.host if request.client else "unknown"
        key = f"{minute}:{hashlib.sha256(client.encode()).hexdigest()}"
        insert = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
        with sessions.begin() as db:
            statement = insert(RateBucket).values(key=key, count=1)
            count = db.scalar(
                statement.on_conflict_do_update(
                    index_elements=["key"], set_={"count": RateBucket.count + 1}
                ).returning(RateBucket.count)
            )
            db.execute(delete(RateBucket).where(RateBucket.key < f"{minute - 2}:"))
        if count > rate_limit:
            raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": "60"})

    def view(link: Link, request: Request):
        return LinkView(
            code=link.code,
            url=link.url,
            short_url=str(request.url_for("redirect", code=link.code)),
            created_at=utc(link.created_at),
        )

    @app.get("/health")
    def health():
        with engine.connect() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok", "service": "shortener"}

    @app.post(
        "/api/links",
        status_code=201,
        response_model=LinkView,
        dependencies=[Depends(authenticated), Depends(limited)],
    )
    def create_link(
        body: LinkCreate,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128)] = None,
    ):
        destination = str(body.url)
        request_hash = hashlib.sha256(
            json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()

        # FEATURE_POLICY: agreed abuse controls are inserted here.
        def existing_result(db):
            previous = (
                db.scalar(select(Link).where(Link.idempotency_key == idempotency_key))
                if idempotency_key
                else None
            )
            if previous:
                # FEATURE_REPLAY: additional request fields must match on replay.
                if previous.request_hash != request_hash:
                    raise HTTPException(409, "Idempotency key already used for a different request")
                response.status_code = 200
                return view(previous, request)
            return None

        for _ in range(5):
            with sessions() as db:
                previous = existing_result(db)
                if previous:
                    return previous
                link = Link(
                    code=secrets.token_urlsafe(6),
                    url=destination,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                )
                # FEATURE_CREATE: workflow scenarios initialize new fields here.
                db.add(link)
                try:
                    db.commit()
                    return view(link, request)
                except IntegrityError:
                    db.rollback()
                    previous = existing_result(db)
                    if previous:
                        return previous
        raise HTTPException(503, "Could not allocate a short code; please retry")

    @app.get("/r/{code}", name="redirect", status_code=302)
    def redirect(code: str):
        with sessions.begin() as db:
            link = db.get(Link, code)
            if not link:
                raise HTTPException(404, "Link not found")
            # FEATURE_REDIRECT: workflow scenarios add expiration checks here.
            db.add(Click(code=code))
            destination = link.url
        return RedirectResponse(destination, status_code=302, headers={"Cache-Control": "no-store"})

    @app.get("/api/links/{code}/stats", dependencies=[Depends(authenticated)])
    def stats(code: str):
        with sessions() as db:
            if not db.get(Link, code):
                raise HTTPException(404, "Link not found")
            rows = db.execute(
                select(func.date(Click.occurred_at), func.count())
                .where(Click.code == code)
                .group_by(func.date(Click.occurred_at))
                .order_by(func.date(Click.occurred_at))
            ).all()
        return {
            "code": code,
            "total_clicks": sum(count for _, count in rows),
            "daily": [{"date": str(day), "clicks": count} for day, count in rows],
        }

    return app
