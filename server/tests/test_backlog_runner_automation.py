"""BacklogRunner automation: identity, config schema, and registry presence."""
from __future__ import annotations


def test_id_and_name():
    from server.automations.backlog_runner import BacklogRunner
    br = BacklogRunner()
    assert br.id == "backlog-runner"
    assert br.name == "Backlog Runner"


def test_modes():
    """Phase 1: propose-vs-auto is a PRODUCT decision now (``Project.automation_policy``),
    so the global runner knob is only whether it runs at all."""
    from server.automations.backlog_runner import BacklogRunner
    assert BacklogRunner().modes == ("off", "on")


def test_config_schema_keys_and_types():
    from server.automations.backlog_runner import BacklogRunner
    keys = {f.key: f for f in BacklogRunner().config_schema}
    assert keys["max_concurrent"].type == "int"
    assert keys["max_concurrent"].default == 1
    assert keys["max_spawns_per_day"].type == "int"
    assert keys["max_spawns_per_day"].default == 10
    assert keys["max_issues_per_pr"].type == "int"
    assert keys["max_issues_per_pr"].default == 1


def test_per_product_knobs_left_the_global_schema():
    """Every knob the product policy now owns is gone from the runner's own
    schema — one schema per concept (constitution VI), never a global copy the
    product's answer can disagree with."""
    from server.automations.backlog_runner import BacklogRunner
    keys = {f.key for f in BacklogRunner().config_schema}
    for gone in ("enabled_repos", "repo_weights", "repo_caps", "queue_min_status",
                 "max_attempts", "default_model", "respect_dependencies"):
        assert gone not in keys
    # …and what stays global (one runner-wide budget) is still there.
    for kept in ("selection_strategy", "max_concurrent", "max_spawns_per_day",
                 "max_issues_per_pr", "grouping_interval_minutes"):
        assert kept in keys
    # needs_human_retry_days retired (spec 2026-09-18 §3): a hold releases on a
    # human signal, never on a timer.
    assert "needs_human_retry_days" not in keys


def test_retired_keys_cover_the_label_gate_and_the_per_product_knobs():
    """The label gate died with the GitHub label search (#990) — board status,
    not a label, says what is workable now — and the repo knobs died with the
    product policy. Dropping a field alone would make every stored row that still
    carries it invalid, so each must be RETIRED: the key stays listed, and the row
    keeps the rest of its settings."""
    from server.automations.backlog_runner import BacklogRunner
    br = BacklogRunner()
    assert br.retired_config_keys == (
        "eligible_label", "enabled_repos", "repo_weights", "repo_caps",
        "queue_min_status", "max_attempts", "default_model", "respect_dependencies",
        "needs_human_retry_days")
    assert not set(br.retired_config_keys) & {f.key for f in br.config_schema}


def test_status_unavailable_without_measurements():
    from server.automations.backlog_runner import BacklogRunner
    s = BacklogRunner().status(ctx=None)
    assert s.level == "unknown"
    assert "unavailable" in s.text
    assert isinstance(s.text, str)


def test_registered_in_registry():
    from server.automations.registry import registry
    assert "backlog-runner" in [a.id for a in registry.all()]


def test_status_carries_blocked_and_untriaged_in_counts_not_text():
    """``blocked``/``untriaged`` used to be spelled out in ``status().text``; the
    web now renders ``counts`` as stat tiles (spec 2026-09-28 §2), so the text
    stays terse and the values only need to survive into ``counts``."""
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(backlog_counts={"in_flight": 1, "eligible": 0,
                                          "blocked": 3, "untriaged": 2})
    status = BacklogRunner().status(ctx)
    assert "blocked" not in status.text and "untriaged" not in status.text
    assert status.counts["blocked"] == 3
    assert status.counts["untriaged"] == 2


# --------------------------------------------------------------------------- #
# Per-product status lines (spec §9)                                          #
# --------------------------------------------------------------------------- #

def test_status_appends_a_line_per_product_with_dispatch_on():
    """The card answers "which products is it working, and how hard" — one
    segment per ON product. The web renders ``status.text`` without honouring
    newlines, so the segments join the summary on ONE line."""
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(
        backlog_counts={"in_flight": 1, "eligible": 12, "blocked": 0},
        backlog_products={
            "kwgt-cairn": {"dispatch": "auto", "eligible": 12, "blocked": 0,
                           "untriaged": 0, "in_flight": 1, "cap": 1},
            "kindash": {"dispatch": "propose", "eligible": 3, "blocked": 0,
                        "untriaged": 0, "in_flight": 0, "cap": 2},
        })
    text = BacklogRunner().status(ctx).text
    assert "kwgt-cairn: auto · 1/1 in flight · 12 eligible" in text
    assert "kindash: propose · 0/2 in flight · 3 eligible" in text
    assert "\n" not in text


def test_status_appends_bounced_count_and_publishes_products():
    """D8: the card's per-product segment gains "N bounced" when the product has
    any needs_spec skips, and ``AutomationStatus.products`` carries the same rows
    (camelCase, spec §10) for the web's per-product table (Task 5)."""
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(
        backlog_counts={"in_flight": 1, "eligible": 12, "blocked": 0},
        backlog_products={
            "kwgt-cairn": {"dispatch": "auto", "eligible": 12, "blocked": 0,
                           "untriaged": 0, "in_flight": 1, "cap": 1, "bounced": 2},
        })
    status = BacklogRunner().status(ctx)
    assert "kwgt-cairn: auto · 1/1 in flight · 12 eligible · 2 bounced" in status.text
    assert status.products == [{"slug": "kwgt-cairn", "dispatch": "auto", "inFlight": 1,
                                "cap": 1, "eligible": 12, "bounced": 2, "held": 0,
                                "blocked": 0, "skipped": {}}]


def test_status_omits_bounced_segment_when_zero():
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(
        backlog_counts={"in_flight": 1, "eligible": 12, "blocked": 0},
        backlog_products={
            "kwgt-cairn": {"dispatch": "auto", "eligible": 12, "blocked": 0,
                           "untriaged": 0, "in_flight": 1, "cap": 1, "bounced": 0},
        })
    text = BacklogRunner().status(ctx).text
    assert "bounced" not in text


def test_status_omits_products_with_dispatch_off():
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(
        backlog_counts={"in_flight": 1, "eligible": 2, "blocked": 0},
        backlog_products={
            "on-one": {"dispatch": "auto", "eligible": 2, "blocked": 0,
                       "untriaged": 0, "in_flight": 1, "cap": 1},
            "off-one": {"dispatch": "off", "eligible": 9, "blocked": 0,
                        "untriaged": 0, "in_flight": 0, "cap": 1},
        })
    text = BacklogRunner().status(ctx).text
    assert "on-one" in text and "off-one" not in text


def test_status_says_so_when_no_product_has_dispatch_on():
    """An enabled runner with nothing opted in used to read "0 eligible" — an
    empty backlog rather than a policy nobody turned on."""
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(
        backlog_counts={"in_flight": 0, "eligible": 0, "blocked": 0},
        backlog_products={})
    status = BacklogRunner().status(ctx)
    assert status.text == "idle — no product has dispatch on"
    assert status.level == "idle"
    # ``[]`` — "this automation HAS products, none are on" — not ``None``, which
    # is reserved for an automation with no concept of products (PR Doctor). The
    # web renders the two differently (empty table vs no section at all).
    assert status.products == []


def test_status_says_unavailable_when_the_count_read_failed():
    """"We don't know" and "no product is opted in" are DIFFERENT sentences: a
    ``None`` product map (the count seam's failed-read answer) must not render as
    the idle text, or an operator debugging a broken mirror read is told their
    policies are unset."""
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(
        backlog_counts={"in_flight": 2, "eligible": 0, "blocked": 0},
        backlog_products=None)
    status = BacklogRunner().status(ctx)
    assert status.text == ("2 work in flight · product counts unavailable "
                           "(the queue read failed)")
    assert status.level == "warn"
    assert status.text != "idle — no product has dispatch on"


def test_status_without_the_products_cache_is_unchanged():
    """A tick that never reached the count seam leaves no ``backlog_products``;
    the card must keep rendering the summary rather than claiming nothing is on.
    The counts themselves are tiles on the web (spec 2026-09-28 §2), so the text
    names only the in-flight number and keeps every count in ``counts``."""
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(backlog_counts={"in_flight": 1, "eligible": 2, "blocked": 0})
    status = BacklogRunner().status(ctx)
    assert status.text == "1 work in flight"
    assert status.counts == {"in_flight": 1, "eligible": 2, "blocked": 0}


def test_status_names_missing_queue_counts_rather_than_hiding_them():
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(backlog_counts={"in_flight": 1, "needs_human": 3})
    assert BacklogRunner().status(ctx).text == "1 work in flight · queue counts unavailable"


# --------------------------------------------------------------------------- #
# Queue provenance on the status card (#990)                                  #
# --------------------------------------------------------------------------- #

def test_status_appends_pick_text_when_present_and_not_idle():
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(
        backlog_counts={"in_flight": 1, "eligible": 2, "blocked": 0},
        backlog_last_pick={"ref": "o/r#1", "score": 12.5, "product": "widget",
                           "over": ["o/r#2", "o/r#3"]})
    text = BacklogRunner().status(ctx).text
    assert text.endswith(
        " · picked o/r#1 (score 12.5, product widget) over o/r#2, o/r#3")


def test_status_unchanged_when_no_pick():
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(backlog_counts={"in_flight": 1, "eligible": 2, "blocked": 0})
    text = BacklogRunner().status(ctx).text
    assert "picked" not in text


def test_status_omits_pick_when_idle():
    """A stale pick from a prior spawn must not clutter the card once the
    runner has gone idle (0 in flight, nothing needing a human)."""
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(
        backlog_counts={"in_flight": 0, "eligible": 2, "blocked": 0, "needs_human": 0},
        backlog_last_pick={"ref": "o/r#1", "score": 12.5, "product": "widget", "over": []})
    status = BacklogRunner().status(ctx)
    assert status.level == "idle"
    assert "picked" not in status.text


def test_format_pick_all_fields():
    from server.automations.backlog_runner import format_pick
    pick = {"ref": "o/r#1", "score": 12.5, "product": "widget",
            "over": ["o/r#2", "o/r#3"]}
    assert format_pick(pick) == "picked o/r#1 (score 12.5, product widget) over o/r#2, o/r#3"


def test_format_pick_score_none():
    from server.automations.backlog_runner import format_pick
    pick = {"ref": "o/r#1", "score": None, "product": "widget", "over": []}
    assert format_pick(pick) == "picked o/r#1 (product widget)"


def test_format_pick_product_none():
    from server.automations.backlog_runner import format_pick
    pick = {"ref": "o/r#1", "score": 5, "product": None, "over": []}
    assert format_pick(pick) == "picked o/r#1 (score 5)"


def test_format_pick_over_empty():
    from server.automations.backlog_runner import format_pick
    pick = {"ref": "o/r#1", "score": 5, "product": "widget", "over": []}
    assert format_pick(pick) == "picked o/r#1 (score 5, product widget)"


def test_format_pick_no_score_no_product_no_over():
    from server.automations.backlog_runner import format_pick
    pick = {"ref": "o/r#1", "score": None, "product": None, "over": []}
    assert format_pick(pick) == "picked o/r#1"


def test_parked_work_remains_visible_when_no_product_has_dispatch_on():
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(
        backlog_counts={"in_flight": 0, "eligible": 0, "needs_human": 2},
        backlog_products={})
    status = BacklogRunner().status(ctx)
    assert status.level == "warn"
    assert status.counts["needs_human"] == 2
    assert "idle" not in status.text


def test_status_products_rows_carry_held_blocked_and_the_skip_map():
    from types import SimpleNamespace

    from server.automations.backlog_runner import BacklogRunner
    ctx = SimpleNamespace(
        backlog_counts={"in_flight": 1, "eligible": 12, "held": 4, "blocked": 2},
        backlog_products={
            "kwgt-cairn": {"dispatch": "auto", "eligible": 12, "blocked": 2,
                           "untriaged": 0, "bounced": 0, "held": 4,
                           "skipped": {"held": 4, "blocked": 2},
                           "in_flight": 1, "cap": 1}})
    status = BacklogRunner().status(ctx)
    assert status.products == [{"slug": "kwgt-cairn", "dispatch": "auto", "inFlight": 1,
                                "cap": 1, "eligible": 12, "bounced": 0, "held": 4,
                                "blocked": 2, "skipped": {"held": 4, "blocked": 2}}]
    # The counts are tiles, not text (spec 2026-09-28 §2): neither held nor
    # needs-human is spelled in the sentence.
    assert "held" not in status.text and "needs" not in status.text
