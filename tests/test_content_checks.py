"""parapetai_agent/content_checks.py -- the actual tier-2 parsing step (real
regex/checksum entity detection), independent of any framework adapter.
No agent_framework/agent_framework extra needed here, unlike test_maf.py --
this module has zero optional dependencies (see its own module docstring
for why the regex_entities backend was chosen specifically to avoid
needing one)."""

from __future__ import annotations

import json

from parapetai_agent.content_checks import ContentCheckConfig
from parapetai_agent.providers.parsers import Snapshot


def _bundle(entries: list[dict[str, object]]) -> dict[str, str]:
    return {"content_checks.json": json.dumps(entries)}


class TestRegexEntitiesScanner:
    def test_ssn_detected(self) -> None:
        cc = ContentCheckConfig()
        cc.load_from_bundle(
            _bundle(
                [
                    {
                        "library_id": "pii-ssn",
                        "scanner_id": "regex_entities",
                        "entity_types": ["US_SSN"],
                        "context_key": "content_checks_pii_types",
                    }
                ]
            )
        )
        result = cc.evaluate(
            Snapshot(
                provider="openai",
                endpoint="test",
                messages_preview="my SSN is 123-45-6789, please help",
            )
        )
        assert result.errors == ()
        assert result.context == {"content_checks_pii_types": ["US_SSN"]}

    def test_clean_text_finds_nothing(self) -> None:
        cc = ContentCheckConfig()
        cc.load_from_bundle(
            _bundle(
                [
                    {
                        "library_id": "pii-ssn",
                        "scanner_id": "regex_entities",
                        "entity_types": ["US_SSN"],
                        "context_key": "content_checks_pii_types",
                    }
                ]
            )
        )
        result = cc.evaluate(
            Snapshot(
                provider="openai", endpoint="test", messages_preview="what's the weather today?"
            )
        )
        assert result.errors == ()
        assert result.context == {"content_checks_pii_types": []}

    def test_valid_credit_card_detected_via_luhn(self) -> None:
        # 4111111111111111 is the standard Visa test number -- passes Luhn.
        cc = ContentCheckConfig()
        cc.load_from_bundle(
            _bundle(
                [
                    {
                        "library_id": "pii-cc",
                        "scanner_id": "regex_entities",
                        "entity_types": ["CREDIT_CARD"],
                        "context_key": "content_checks_pii_types",
                    }
                ]
            )
        )
        result = cc.evaluate(
            Snapshot(
                provider="openai",
                endpoint="test",
                messages_preview="card number 4111111111111111 please",
            )
        )
        assert result.context["content_checks_pii_types"] == ["CREDIT_CARD"]

    def test_digit_run_failing_luhn_is_not_flagged_as_a_card(self) -> None:
        """The whole reason CREDIT_CARD needs more than a bare digit-count
        regex: an arbitrary 16-digit run (an order id, a padded reference
        number) must not false-positive just because it's card-shaped."""
        cc = ContentCheckConfig()
        cc.load_from_bundle(
            _bundle(
                [
                    {
                        "library_id": "pii-cc",
                        "scanner_id": "regex_entities",
                        "entity_types": ["CREDIT_CARD"],
                        "context_key": "content_checks_pii_types",
                    }
                ]
            )
        )
        result = cc.evaluate(
            Snapshot(
                provider="openai",
                endpoint="test",
                messages_preview="order reference 1234567890123456",
            )
        )
        assert result.context["content_checks_pii_types"] == []

    def test_aws_access_key_detected(self) -> None:
        cc = ContentCheckConfig()
        cc.load_from_bundle(
            _bundle(
                [
                    {
                        "library_id": "secrets-aws",
                        "scanner_id": "regex_entities",
                        "entity_types": ["AWS_ACCESS_KEY"],
                        "context_key": "content_checks_secrets",
                    }
                ]
            )
        )
        result = cc.evaluate(
            Snapshot(
                provider="openai",
                endpoint="test",
                messages_preview="key is AKIAIOSFODNN7EXAMPLE, don't tell anyone",
            )
        )
        assert result.context == {"content_checks_secrets": ["AWS_ACCESS_KEY"]}

    def test_multiple_entries_sharing_a_context_key_are_unioned(self) -> None:
        cc = ContentCheckConfig()
        cc.load_from_bundle(
            _bundle(
                [
                    {
                        "library_id": "pii-ssn",
                        "scanner_id": "regex_entities",
                        "entity_types": ["US_SSN"],
                        "context_key": "content_checks_pii_types",
                    },
                    {
                        "library_id": "pii-email",
                        "scanner_id": "regex_entities",
                        "entity_types": ["EMAIL_ADDRESS"],
                        "context_key": "content_checks_pii_types",
                    },
                ]
            )
        )
        result = cc.evaluate(
            Snapshot(
                provider="openai",
                endpoint="test",
                messages_preview="SSN 123-45-6789, email me at bob@example.com",
            )
        )
        assert result.context["content_checks_pii_types"] == ["EMAIL_ADDRESS", "US_SSN"]


class TestEmptyOrMissingConfigIsHarmless:
    def test_no_content_checks_file_means_zero_entries(self) -> None:
        cc = ContentCheckConfig()
        cc.load_from_bundle({"00-base.cedar": "permit(principal, action, resource);"})
        result = cc.evaluate(
            Snapshot(provider="openai", endpoint="test", messages_preview="my SSN is 123-45-6789")
        )
        assert result == cc.evaluate(
            Snapshot(provider="openai", endpoint="test", messages_preview="anything at all")
        )
        assert result.errors == ()
        assert result.context == {}

    def test_never_configured_evaluate_is_a_no_op(self) -> None:
        cc = ContentCheckConfig()
        result = cc.evaluate(
            Snapshot(provider="openai", endpoint="test", messages_preview="my SSN is 123-45-6789")
        )
        assert result.errors == ()
        assert result.context == {}


class TestFailClosedOnBadConfigOrUnknownScanner:
    def test_malformed_json_keeps_previous_good_entries(self) -> None:
        """Mirrors PolicyEngine.load_from_bundle()'s own contract: a
        malformed bundle is REJECTED, never silently emptying what was
        already configured."""
        cc = ContentCheckConfig()
        cc.load_from_bundle(
            _bundle(
                [
                    {
                        "library_id": "pii-ssn",
                        "scanner_id": "regex_entities",
                        "entity_types": ["US_SSN"],
                        "context_key": "content_checks_pii_types",
                    }
                ]
            )
        )
        cc.load_from_bundle({"content_checks.json": "{not valid json"})
        result = cc.evaluate(
            Snapshot(provider="openai", endpoint="test", messages_preview="SSN 123-45-6789")
        )
        assert result.context == {"content_checks_pii_types": ["US_SSN"]}

    def test_unknown_scanner_id_is_a_hard_error_not_a_silent_skip(self) -> None:
        """The exact case content_checks.py's own module docstring flags:
        a configured-but-unrunnable scanner must surface as `errors`, not
        an empty/absent context.content_checks key -- the caller (maf.py)
        is what turns this into a deny before Cedar ever runs."""
        cc = ContentCheckConfig()
        cc.load_from_bundle(
            _bundle(
                [
                    {
                        "library_id": "future-ml-scanner",
                        "scanner_id": "some_future_ml_backend",
                        "entity_types": ["TOXICITY"],
                        "context_key": "content_checks_toxicity",
                    }
                ]
            )
        )
        result = cc.evaluate(
            Snapshot(provider="openai", endpoint="test", messages_preview="anything")
        )
        assert result.context == {}
        assert len(result.errors) == 1
        assert "some_future_ml_backend" in result.errors[0]

    def test_unknown_entity_type_is_skipped_not_an_error(self) -> None:
        """Narrower than an unknown scanner_id: one entity type this SDK
        doesn't recognise yet shouldn't fail the whole entry when other
        entity types in the same list ARE runnable."""
        cc = ContentCheckConfig()
        cc.load_from_bundle(
            _bundle(
                [
                    {
                        "library_id": "pii-mixed",
                        "scanner_id": "regex_entities",
                        "entity_types": ["US_SSN", "SOME_FUTURE_ENTITY_TYPE"],
                        "context_key": "content_checks_pii_types",
                    }
                ]
            )
        )
        result = cc.evaluate(
            Snapshot(provider="openai", endpoint="test", messages_preview="SSN 123-45-6789")
        )
        assert result.errors == ()
        assert result.context == {"content_checks_pii_types": ["US_SSN"]}
