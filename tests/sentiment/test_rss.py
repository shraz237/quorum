"""Tests for the RSS sentiment classifier."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_message(text: str) -> MagicMock:
    """Build a mock anthropic Messages response with a single content block."""
    content_block = MagicMock()
    content_block.text = text
    message = MagicMock()
    message.content = [content_block]
    return message


def _reload_rss():
    """Force-reload sources.rss and return classify_article + _HAIKU_MODEL."""
    mod_name = "sources.rss"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    from sources.rss import classify_article, _HAIKU_MODEL  # type: ignore[import]
    return classify_article, _HAIKU_MODEL


# ---------------------------------------------------------------------------
# Tests for classify_article
# ---------------------------------------------------------------------------


class TestClassifyArticle:
    """Unit tests for classify_article(), with the Anthropic client mocked."""

    def test_bullish_classification(self):
        response_json = '{"sentiment": "bullish", "score": 0.75, "relevance": 0.9}'
        mock_message = _make_message(response_json)

        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.return_value = mock_message

            classify_article, _ = _reload_rss()
            result = classify_article("OPEC cuts boost oil prices", "reuters")

        assert result["sentiment"] == "bullish"
        assert result["score"] == pytest.approx(0.75)
        assert result["relevance"] == pytest.approx(0.9)

    def test_bearish_classification(self):
        response_json = '{"sentiment": "bearish", "score": -0.5, "relevance": 0.8}'
        mock_message = _make_message(response_json)

        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.return_value = mock_message

            classify_article, _ = _reload_rss()
            result = classify_article("Recession fears hammer crude oil", "oilprice")

        assert result["sentiment"] == "bearish"
        assert result["score"] == pytest.approx(-0.5)
        assert result["relevance"] == pytest.approx(0.8)

    def test_neutral_classification(self):
        response_json = '{"sentiment": "neutral", "score": 0.0, "relevance": 0.4}'
        mock_message = _make_message(response_json)

        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.return_value = mock_message

            classify_article, _ = _reload_rss()
            result = classify_article("Markets await Fed decision", "reuters")

        assert result["sentiment"] == "neutral"
        assert result["score"] == pytest.approx(0.0)
        assert result["relevance"] == pytest.approx(0.4)

    def test_api_error_returns_fallback(self):
        """When Anthropic raises, classify_article must return neutral/0/0."""
        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.side_effect = RuntimeError("network error")

            classify_article, _ = _reload_rss()
            result = classify_article("Some headline", "reuters")

        assert result == {"sentiment": "neutral", "score": 0.0, "relevance": 0.0}

    def test_malformed_json_returns_fallback(self):
        """When Haiku returns invalid JSON, classify_article must return neutral."""
        mock_message = _make_message("not valid json {{{{")

        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.return_value = mock_message

            classify_article, _ = _reload_rss()
            result = classify_article("Some headline", "reuters")

        assert result == {"sentiment": "neutral", "score": 0.0, "relevance": 0.0}

    def test_uses_haiku_model(self):
        """classify_article must call the Haiku model, not Opus/Sonnet."""
        response_json = '{"sentiment": "neutral", "score": 0.0, "relevance": 0.5}'
        mock_message = _make_message(response_json)

        with patch("anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.return_value = mock_message

            classify_article, haiku_model = _reload_rss()
            classify_article("Oil steady", "reuters")

            assert mock_client.messages.create.call_args.kwargs["model"] == haiku_model
