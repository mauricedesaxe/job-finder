# pyright: reportPrivateUsage=false
from __future__ import annotations

import pytest
from starlette.datastructures import FormData, UploadFile

from job_finder.review.configuration import (
    _apply_configuration_edit,
    _MalformedConfigurationForm,
    _parse_configuration_form,
    _RawConfigurationForm,
    _RawNamedRow,
    _transform_rows,
)


def test_form_parser_preserves_order_whitespace_blanks_and_duplicates() -> None:
    values: list[tuple[str, str | UploadFile]] = [
        ("search_keywords", " first \r\n\r\n first "),
        ("source_count", "2"),
        ("source_order.0", "lever"),
        ("source_order.1", "unknown"),
        ("source_selected", "lever"),
        ("criterion_count", "1"),
        ("personal_criteria.0.key", " key "),
        ("personal_criteria.0.name", " name "),
        ("personal_criteria.0.instructions", " instructions\n"),
        ("profile_count", "1"),
        ("target_profiles.0.key", "profile"),
        ("target_profiles.0.name", "Profile"),
        ("target_profiles.0.instructions", "Do work"),
        ("expected_draft_version", "7"),
    ]
    form = FormData(values)

    raw = _parse_configuration_form(form)

    assert raw.search_keywords == (" first ", "", " first ")
    assert raw.enabled_sources == ("lever", "unknown")
    assert raw.personal_criteria == (_RawNamedRow(" key ", " name ", " instructions\n"),)
    assert raw.expected_draft_version == "7"


def test_corrected_invalid_source_is_not_dropped_when_its_checkbox_was_absent() -> None:
    form = FormData(
        [
            ("search_keywords", "keyword"),
            ("source_count", "1"),
            ("source_order.0", "ashby"),
            ("source_preserve", "0"),
            ("criterion_count", "0"),
            ("profile_count", "0"),
            ("expected_draft_version", "7"),
        ]
    )

    assert _parse_configuration_form(form).enabled_sources == ("ashby",)


def test_source_removal_uses_submitted_position_before_filtering_unchecked_sources() -> None:
    form = FormData(
        [
            ("search_keywords", "keyword"),
            ("source_count", "3"),
            ("source_order.0", "ashby"),
            ("source_order.1", "lever"),
            ("source_order.2", "unknown"),
            ("source_selected", "lever"),
            ("source_preserve", "2"),
            ("criterion_count", "0"),
            ("profile_count", "0"),
            ("expected_draft_version", "7"),
        ]
    )

    raw = _apply_configuration_edit(form, "source.remove.2")

    assert raw.enabled_sources == ("lever",)


def test_named_and_source_row_actions_preserve_exact_values() -> None:
    raw = _RawConfigurationForm(
        search_keywords=(" one ", "two"),
        enabled_sources=("ashby",),
        personal_criteria=(
            _RawNamedRow("a", "A", "first"),
            _RawNamedRow("b", "B", "second"),
        ),
        target_profiles=(_RawNamedRow("p", "P", "profile"),),
        expected_draft_version="4",
    )

    assert [row.key for row in _transform_rows(raw, "criterion.up.1").personal_criteria] == [
        "b",
        "a",
    ]
    assert _transform_rows(raw, "profile.add").target_profiles[-1] == _RawNamedRow("", "", "")


@pytest.mark.parametrize(
    "form",
    [
        FormData(),
        FormData({"search_keywords": "a", "source_count": "one"}),
        FormData(
            {
                "search_keywords": "a",
                "source_count": "0",
            }
        ),
        FormData(
            {
                "search_keywords": "a",
                "source_count": "0",
                "source_preserve": "0",
            }
        ),
    ],
)
def test_form_parser_rejects_malformed_transport(form: FormData) -> None:
    with pytest.raises(_MalformedConfigurationForm):
        _parse_configuration_form(form)
