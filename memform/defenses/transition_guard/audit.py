"""Compile a cited repair audit into a decision; never infer safety from prose."""
from __future__ import annotations

import copy
from typing import Any

from .interface import Evidence, MemoryTransition
from .citations import observation_quote


AUDIT_SCHEMA = "repair-audit-v3"
CHECKS = ("knowledge_preservation", "claim_support", "gap_resolution",
          "policy_consistency", "unknown_handling", "permission_scope")


def _reason(row: dict[str, Any]) -> bool:
    return isinstance(row.get("reason"), str) and bool(row["reason"].strip())


def cited_records(value: Any, evidence: tuple[Evidence, ...]) -> set[str] | None:
    """Validate EVERY citation, including quotes mixed with otherwise valid ones."""
    if not isinstance(value, list):
        return None
    pool = {e.record_id: e.text for e in evidence}
    found = set()
    for row in value:
        if not isinstance(row, dict):
            return None
        rid, quote = row.get("record_id"), row.get("quote")
        if (not isinstance(rid, str) or rid not in pool or not isinstance(quote, str)
                or observation_quote(pool[rid], quote) is None):
            return None
        found.add(rid)
    return found


def exact_quotes(value: Any, text: str) -> bool:
    return isinstance(value, list) and all(
        isinstance(q, str) and len(q.strip()) >= 12 and q in text for q in value)


def expand_audit(support: dict[str, Any], evidence: tuple[Evidence, ...]) -> tuple[dict[str, Any], list[str]]:
    """Expand compact references, never synthesize a judgment or a supporting quote.

    The model quotes each observation once in citation_table. Citation references
    and repaired-policy unit references are zero-based integers (never booleans).
    Expanded v3 reports remain accepted for offline diagnostics. Old schema
    versions are still rejected by compile_audit.
    """
    report = copy.deepcopy(support)
    errors: list[str] = []
    compact = "citation_table" in report or "report_format" in report
    if not compact:
        return report, errors
    if report.get("report_format") != "compact-v1":
        errors.append("compact:report_format")
    table = report.get("citation_table")
    if not isinstance(table, list):
        errors.append("compact:citation_table")
        table = []
    for index, citation in enumerate(table):
        if not cited_records([citation], evidence):
            errors.append(f"compact:citation_table:{index}:invalid_citation")
    units = report.get("policy_units")
    policy = [unit.get("policy_quote") if isinstance(unit, dict) else None
              for unit in units] if isinstance(units, list) else []

    def reference(value: Any, entries: list, path: str) -> Any:
        if type(value) is not int or not 0 <= value < len(entries):
            errors.append("compact:" + path + ":invalid_reference")
            return None
        return copy.deepcopy(entries[value])

    def visit(value: Any, path: str) -> None:
        if isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}:{index}")
        elif isinstance(value, dict):
            for key, item in list(value.items()):
                if key == "citation_table":
                    continue
                if key == "citations" and isinstance(item, list):
                    value[key] = [reference(ref, table, path + ":citations") for ref in item]
                elif key == "evidence_checks" and isinstance(item, list):
                    assessments = []
                    for index, assessment in enumerate(item):
                        location = f"{path}:evidence_checks:{index}"
                        if not isinstance(assessment, dict):
                            errors.append("compact:" + location + ":invalid_assessment")
                            assessments.append(assessment)
                            continue
                        citation = reference(assessment.get("citation"), table, location)
                        if "record_id" in assessment or "quote" in assessment:
                            errors.append("compact:" + location + ":mixed_citation_formats")
                        expanded = {k: v for k, v in assessment.items() if k != "citation"}
                        if isinstance(citation, dict):
                            expanded.update(record_id=citation.get("record_id"), quote=citation.get("quote"))
                        assessments.append(expanded)
                    value[key] = assessments
                elif key in {"policy_quotes", "uncertainty_quotes"} and isinstance(item, list):
                    # Literal subspans are also allowed; original_quotes always
                    # refer to the original memory and are never unit indices.
                    value[key] = [ref if isinstance(ref, str) else reference(ref, policy, path + ":" + key)
                                  for ref in item]
                else:
                    visit(item, path + ":" + key)

    visit(report, "report")
    checks = report.get("checks")
    if isinstance(checks, list):
        rows = []
        for item in checks:
            if not isinstance(item, list) or len(item) != 2:
                errors.append("compact:invalid_check_pair")
                continue
            name, value = item
            if isinstance(value, str):
                rows.append({"check": name, "status": value, "findings": []})
            elif isinstance(value, dict):
                if "check" in value:
                    errors.append("compact:duplicate_check_name")
                rows.append({**value, "check": name})
            else:
                rows.append({"check": name, "status": None, "findings": None})
        report["checks"] = rows
    else:
        errors.append("compact:checks_not_list")
    return report, errors


def compile_audit(support: dict[str, Any], t: MemoryTransition, memory: str,
                  edits: list[dict[str, Any]], witness: tuple[Evidence, ...]) -> dict[str, Any]:
    """Malformed/contradictory reports are inconclusive, never approved or relabelled safe.

    Whole-policy findings, local edit support and witness coverage are independent
    obligations. A witness need not support an edit, but must cite its own record
    and locate the relevant repaired policy span before it counts as covered.
    """
    evidence = t.selected_experiences + witness
    support, errors = expand_audit(support, evidence)
    failed: list[str] = []
    unknown: list[str] = []
    cited: set[str] = set()
    unsupported_claims, policy_conflicts = [], []
    if support.get("schema_version") != AUDIT_SCHEMA:
        errors.append("schema_version")
    # Do not silently discard a conflicting legacy verdict or negative boolean.
    legacy = {"verdict", "preserves_supported_knowledge", "no_invented_facts", "resolves_gap",
              "policy_conflict_free", "unknowns_preserved", "no_single_success_generalization",
              "policy_conflicts", "unsupported_claims", "uncertainty_explicit"}
    if legacy & support.keys():
        errors.append("legacy_or_duplicate_decision_fields")

    checks = support.get("checks")
    seen = set()
    if not isinstance(checks, list):
        errors.append("checks_not_list")
    for row in checks if isinstance(checks, list) else []:
        name = row.get("check") if isinstance(row, dict) else None
        if not isinstance(name, str) or name not in CHECKS or name in seen:
            errors.append("unknown_or_duplicate_check")
            continue
        seen.add(name)
        status, findings = row.get("status"), row.get("findings")
        if status not in ("pass", "fail", "unknown") or not isinstance(findings, list):
            errors.append(name + ":invalid_status_or_findings")
            continue
        if status in ("pass", "unknown") and findings:
            errors.append(name + ":nonfailure_with_findings")
        if status == "fail" and not findings:
            errors.append(name + ":failure_without_finding")
        for finding in findings:
            if not isinstance(finding, dict):
                errors.append(name + ":invalid_finding")
                continue
            quotes = finding.get("policy_quotes")
            original = finding.get("original_quotes")
            cites = cited_records(finding.get("citations"), evidence)
            if (not exact_quotes(quotes, memory) or not exact_quotes(original, t.proposed_memory)
                    or not cites or not isinstance(finding.get("reason"), str) or not finding["reason"].strip()
                    or (name == "knowledge_preservation" and not original)
                    or (name != "knowledge_preservation" and not quotes)
                    or (name == "policy_consistency" and len(set(quotes or [])) < 2)):
                errors.append(name + ":unlocated_or_uncited_finding")
            else:
                cited.update(cites)
        if status == "fail":
            failed.append(name)
            if name == "claim_support":
                unsupported_claims.extend(findings)
            if name == "policy_consistency":
                policy_conflicts.extend(findings)
        elif status == "unknown":
            unknown.append(name)
    errors.extend("missing_check:" + name for name in CHECKS if name not in seen)

    audits = support.get("edit_checks")
    audits_ok = isinstance(audits, list) and len(audits) == len(edits)
    if not audits_ok:
        errors.append("edit_check_count")
    audited_indices = set()
    for row in audits if isinstance(audits, list) else []:
        index = row.get("index") if isinstance(row, dict) else None
        if type(index) is not int or not 0 <= index < len(edits) or index in audited_indices:
            errors.append("invalid_or_duplicate_edit_index")
            audits_ok = False
            continue
        audited_indices.add(index)
        status, permission = row.get("status"), row.get("introduces_permission")
        cites = cited_records(row.get("citations"), evidence)
        if status not in ("supported", "unsupported", "inconclusive") or type(permission) is not bool or cites is None:
            errors.append(f"edit:{index}:invalid_schema_or_citation")
            audits_ok = False
            continue
        cited.update(cites)
        if status == "supported":
            if len(cites) < (2 if permission else 1):
                unknown.append(f"edit:{index}:insufficient_support")
                audits_ok = False
        else:
            audits_ok = False
            if not isinstance(row.get("reason"), str) or not row["reason"].strip():
                errors.append(f"edit:{index}:missing_reason")
            if status == "unsupported":
                if not cites:
                    errors.append(f"edit:{index}:uncited_rejection")
                failed.append(f"edit:{index}")
            else:
                unknown.append(f"edit:{index}")

    # An independent source review gives selected observations their own place
    # in the report; they need not be misused as support for a witness-only edit.
    selected_review = support.get("selected_evidence_review")
    selected_ids = {e.record_id for e in t.selected_experiences}
    selected_ok = False
    if not isinstance(selected_review, dict):
        errors.append("selected_evidence_review:missing")
    else:
        source_cites = cited_records(selected_review.get("citations"), t.selected_experiences)
        source_quotes = selected_review.get("policy_quotes")
        source_status = selected_review.get("status")
        if (not source_cites or not exact_quotes(source_quotes, memory) or not source_quotes
                or source_status not in ("preserved", "qualified", "unsupported", "inconclusive")
                or (source_status in {"unsupported", "inconclusive"} and not _reason(selected_review))):
            errors.append("selected_evidence_review:invalid_or_unlocated")
        else:
            cited.update(source_cites)
            selected_ok = source_status in ("preserved", "qualified")
            if source_status == "unsupported":
                failed.append("selected_evidence_review")
            elif source_status == "inconclusive":
                unknown.append("selected_evidence_review")

    # Ordered units must cover the entire repaired policy, including unchanged
    # permissions. Positive support is counted per action branch, never pooled
    # across an edit containing several different actions/conditions.
    units = support.get("policy_units")
    units_ok = isinstance(units, list) and bool(units)
    if not units_ok:
        errors.append("policy_units:missing")
    cursor, permission_count = 0, 0
    for index, unit in enumerate(units if isinstance(units, list) else []):
        key = f"policy_unit:{index}"
        if not isinstance(unit, dict):
            errors.append(key + ":invalid_schema")
            units_ok = False
            continue
        quote, kind = unit.get("policy_quote"), unit.get("kind")
        while (cursor < len(memory) and memory[cursor].isspace()
               and not (isinstance(quote, str) and memory.startswith(quote, cursor))):
            cursor += 1
        if (not isinstance(quote, str) or not quote.strip() or not memory.startswith(quote, cursor)
                or kind not in ("permission", "observation", "uncertainty")):
            errors.append(key + ":invalid_location_or_kind")
            units_ok = False
            continue
        cursor += len(quote)
        if kind != "permission":
            continue
        permission_count += 1
        status, introduced = unit.get("status"), unit.get("introduces_permission")
        assessments = unit.get("evidence_checks")
        if (status not in ("supported", "unsupported", "inconclusive") or type(introduced) is not bool
                or not isinstance(assessments, list) or not assessments
                or (status != "supported" and not _reason(unit))):
            errors.append(key + ":invalid_permission_audit")
            units_ok = False
            continue
        positive, opposing, uncertain = set(), set(), set()
        seen_records = set()
        for assessment in assessments:
            relation = assessment.get("relation") if isinstance(assessment, dict) else None
            ids = cited_records([assessment], evidence)
            if (not ids or ids & seen_records
                    or relation not in ("supports", "contradicts", "context_only", "uncertain")
                    or (relation in {"contradicts", "uncertain"} and not _reason(assessment))):
                errors.append(key + ":invalid_evidence_assessment")
                units_ok = False
                continue
            seen_records.update(ids)
            cited.update(ids)
            if relation == "supports":
                positive.update(ids)
            elif relation == "contradicts":
                opposing.update(ids)
            elif relation == "uncertain":
                uncertain.update(ids)
        if status == "unsupported":
            failed.append(key)
            units_ok = False
        elif status == "inconclusive":
            unknown.append(key)
            units_ok = False
        # All executable units here are reusable permissions. An unsupported
        # original fallback must not escape merely by being labelled unchanged.
        # A single successful action can remain as an observation instead.
        elif opposing or uncertain or len(positive) < 2:
            unknown.append(key + ":insufficient_branch_support")
            units_ok = False
    if memory[cursor:].strip():
        errors.append("policy_units:incomplete_coverage")
        units_ok = False

    uncertainty = support.get("uncertainty_quotes")
    uncertainty_ok = exact_quotes(uncertainty, memory)
    if not uncertainty_ok:
        errors.append("invalid_uncertainty_quotes")
    conflicts = support.get("evidence_conflicts")
    if not isinstance(conflicts, list):
        errors.append("evidence_conflicts_not_list")
    for conflict in conflicts if isinstance(conflicts, list) else []:
        cites = cited_records(conflict.get("citations"), evidence) if isinstance(conflict, dict) else None
        if (not cites or len(cites) < 2 or not isinstance(conflict.get("reason"), str)
                or not conflict["reason"].strip()):
            errors.append("invalid_evidence_conflict")
        else:
            cited.update(cites)
    if conflicts and not (uncertainty_ok and uncertainty):
        unknown.append("historical_conflict_without_uncertainty")
        uncertainty_ok = False

    witnesses = support.get("witness_checks")
    required = {e.record_id for e in witness}
    covered: set[str] = set()
    witnesses_ok = isinstance(witnesses, list) and len(witnesses) == len(required)
    if not witnesses_ok:
        errors.append("witness_check_count")
    for row in witnesses if isinstance(witnesses, list) else []:
        rid = row.get("record_id") if isinstance(row, dict) else None
        if not isinstance(rid, str) or rid not in required or rid in covered:
            errors.append("invalid_or_duplicate_witness")
            witnesses_ok = False
            continue
        covered.add(rid)
        cites = cited_records(row.get("citations"), evidence)
        quotes, status = row.get("policy_quotes"), row.get("status")
        if (cites is None or rid not in cites or not exact_quotes(quotes, memory)
                or status not in ("consistent", "uncertainty_preserved", "conflict", "insufficient")):
            errors.append("witness:" + rid + ":invalid_citation_or_location")
            witnesses_ok = False
            continue
        cited.update(cites)
        if status != "insufficient" and not quotes:
            errors.append("witness:" + rid + ":missing_policy_location")
            witnesses_ok = False
        if status == "uncertainty_preserved" and not (
                uncertainty_ok and any(q in u or u in q for q in quotes or [] for u in uncertainty or [])):
            errors.append("witness:" + rid + ":unlocated_uncertainty")
            witnesses_ok = False
        if status in ("conflict", "insufficient"):
            witnesses_ok = False
            if not isinstance(row.get("reason"), str) or not row["reason"].strip():
                errors.append("witness:" + rid + ":missing_reason")
            (failed if status == "conflict" else unknown).append("witness:" + rid)
    if covered != required:
        errors.append("missing_witness_checks")
        witnesses_ok = False
    if not cited & selected_ids:
        errors.append("missing_selected_evidence_citation")

    verdict = "inconclusive" if errors else "unsupported" if failed else "inconclusive" if unknown else "supported"
    return {"effective_verdict": verdict, "protocol_errors": errors, "failed_checks": failed,
            "unknown_checks": unknown, "unsupported_claims": unsupported_claims,
            "policy_conflicts": policy_conflicts, "audits_ok": audits_ok,
            "witnesses_ok": witnesses_ok, "uncertainty_ok": uncertainty_ok,
            "selected_evidence_ok": selected_ok, "policy_units_ok": units_ok,
            "permission_units": permission_count,
            "valid_citation_ids": sorted(cited), "word_count": len(memory.split()),
            "effect_disagreements": sum(isinstance(a, dict) and isinstance(a.get("effect"), str)
                                        and a["effect"] != edits[a["index"]].get("effect")
                                        for a in audits if type(a.get("index")) is int and 0 <= a["index"] < len(edits))
                                     if isinstance(audits, list) and all(isinstance(a, dict) for a in audits) else 0}
