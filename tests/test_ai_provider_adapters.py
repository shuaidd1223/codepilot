from __future__ import annotations


def test_api_provider_build_client_uses_registered_adapter(monkeypatch):
    from codepilot.ai_support import provider_adapters
    from codepilot.ai_support.providers import APIProvider

    calls = []
    fake_client = object()

    def _fake_adapter(provider):
        calls.append(provider)
        return fake_client, "fake.endpoint"

    monkeypatch.setitem(provider_adapters.API_CLIENT_ADAPTERS, "fake", _fake_adapter)

    provider = APIProvider(
        name="Fake API",
        provider_type="fake",
        model="fake-model",
        base_url="http://localhost:9999/v1",
    )

    client, endpoint = provider.build_client()

    assert client is fake_client
    assert endpoint == "fake.endpoint"
    assert calls == [provider]
