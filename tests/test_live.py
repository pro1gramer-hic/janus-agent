"""Optional test against a real OpenAI-compatible model. Skipped unless JANUS_LIVE_TEST=1.

Example with Ollama:
    JANUS_LIVE_TEST=1 JANUS_MODEL=qwen2.5-coder:3b JANUS_BASE_URL=http://localhost:11434/v1 pytest -m live
"""

import os

import pytest

from janus_agent import providers
from janus_agent.config import load_config
from janus_agent.orchestrator import Checker
from janus_agent.security.redact import secret_values_from_findings

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("JANUS_LIVE_TEST") != "1", reason="set JANUS_LIVE_TEST=1 to call a real model"),
]


def test_real_model_reduces_findings_without_leaking(example_copy, tmp_path):
    config = load_config(overrides={"provider": os.environ.get("JANUS_PROVIDER", "openai_compat")})
    provider = providers.create(config.model)
    sent = []
    original = provider.complete

    def recording(request):
        sent.append(request.system + "".join(m.content for m in request.messages))
        return original(request)

    provider.complete = recording
    result = Checker(example_copy, config, provider, out=print, apply=False).run()
    assert len(result.findings_after) < len(result.findings_before)
    secrets = secret_values_from_findings(example_copy, result.findings_before)
    assert secrets and not any(s in text for s in secrets for text in sent)
