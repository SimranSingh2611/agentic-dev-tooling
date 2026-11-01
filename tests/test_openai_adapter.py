"""Exercise real LangChain/OpenAI serialization against a mocked HTTP transport.

This verifies integration plumbing, not live model behavior or model quality.
"""

import json

import httpx
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from engineering.agents import Agents, demo_plan
from engineering.schemas import Review
from engineering.worker import work_once
from engineering.workflow import build_graph


def model_response(result, model):
    return httpx.Response(
        200,
        json={
            "id": "resp_mock",
            "object": "response",
            "created_at": 1700000000,
            "status": "completed",
            "model": model,
            "output": [
                {
                    "id": "msg_mock",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": result.model_dump_json(), "annotations": []}
                    ],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
            "parallel_tool_calls": False,
            "tools": [],
            "tool_choice": "auto",
        },
    )


def test_live_path_uses_responses_api_and_parses_structured_outputs(config, store, api, live_patch):
    config.openai_api_key = "test-key-that-is-never-sent-over-the-network"
    requested_schemas = []

    def respond(request):
        assert str(request.url) == "https://api.openai.com/v1/responses"
        body = json.loads(request.content)
        schema = body["text"]["format"]["name"]
        assert body["text"]["format"]["strict"] is True
        assert body["max_output_tokens"] == 12000
        requested_schemas.append(schema)
        results = {
            "Plan": demo_plan("brownfield", ""),
            "Patch": live_patch,
            "Review": Review(passed=True, findings=[]),
        }
        result = results[schema]
        return httpx.Response(
            200,
            json={
                "id": "resp_mock",
                "object": "response",
                "created_at": 1700000000,
                "status": "completed",
                "model": config.openai_model,
                "output": [
                    {
                        "id": "msg_mock",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": result.model_dump_json(),
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
                "parallel_tool_calls": False,
                "tools": [],
                "tool_choice": "auto",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        agents = Agents(config)
        agents._model = ChatOpenAI(
            model=config.openai_model,
            api_key=config.openai_api_key,
            use_responses_api=True,
            max_retries=0,
            max_tokens=12000,
            timeout=config.model_timeout,
            http_client=http_client,
        )
        graph = build_graph(config, store, InMemorySaver(), agents=agents)
        response = api.post(
            "/engineering/runs",
            json={
                "requirement": "Add optional expiration dates to links.",
                "mode": "live",
                "scenario": "brownfield",
            },
        )
        run_id = response.json()["id"]
        work_once(config, store, graph)
    run = store.get(run_id)
    assert run.status == "awaiting_approval", run.state
    assert requested_schemas == ["Plan", "Patch", "Patch", "Review"]
    assert run.state["model_calls"] == 4
    assert config.openai_api_key not in json.dumps(run.state)


def test_live_clarification_request_carries_prior_questions_and_human_decisions(
    config, store, api, live_patch
):
    config.openai_api_key = "test-key-used-only-in-mocked-transport"
    plans = []
    human_answer = "Include expires_at in LinkView, use UTC and null for no expiration, and keep configuration create-only."
    question = "Should expires_at be included in LinkView?"

    def respond(request):
        body = json.loads(request.content)
        schema = body["text"]["format"]["name"]
        if schema == "Plan":
            message = next(item for item in body["input"] if item.get("role") == "user")
            content = message["content"]
            text = (
                content
                if isinstance(content, str)
                else "".join(part.get("text", "") for part in content)
            )
            payload = json.loads(text)
            plans.append(payload)
            result = demo_plan("brownfield", "")
            if len(plans) == 1:
                result.questions = [question]
            else:
                assert payload["previous_plan"]["questions"] == [question]
                assert payload["clarification_history"] == [
                    {"questions": [question], "answer": human_answer}
                ]
                assert payload["clarification"] == human_answer
        elif schema == "Patch":
            result = live_patch
        else:
            assert schema == "Review"
            result = Review(passed=True, findings=[])
        return model_response(result, config.openai_model)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http_client:
        agents = Agents(config)
        agents._model = ChatOpenAI(
            model=config.openai_model,
            api_key=config.openai_api_key,
            use_responses_api=True,
            max_retries=0,
            http_client=http_client,
        )
        graph = build_graph(config, store, InMemorySaver(), agents=agents)
        response = api.post(
            "/engineering/runs",
            json={
                "requirement": "Add optional expiration dates to links.",
                "scenario": "brownfield",
                "mode": "live",
            },
        )
        run_id = response.json()["id"]
        work_once(config, store, graph)
        assert store.get(run_id).status == "awaiting_clarification"
        response = api.post(
            f"/engineering/runs/{run_id}/clarification",
            json={"revision": 1, "answer": human_answer},
        )
        assert response.status_code == 202
        work_once(config, store, graph)
    run = store.get(run_id)
    assert run.status == "awaiting_approval", run.state
    assert len(plans) == 2
    assert run.state["model_calls"] == 5
    assert store.audit_valid(run_id)
