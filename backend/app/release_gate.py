"""The book-level quality status, and the rule that decides whether it may ship.

States, in order: ``PENDING`` → ``EXTRACTING`` → ``GENERATING`` → ``VALIDATING``
→ ``NEEDS_REVIEW`` / ``BLOCKED`` / ``PASSED`` → ``RELEASED``.

A book is never ``PASSED`` while any of these holds:

* the job is not finished, or a segment is pending, failed or awaiting review;
* extraction reported content as missing (an image page without OCR, no text);
* a segment's audio failed its integrity check or its file is missing;
* the completeness chain (source → units → audio → DTBook → SMIL) is broken;
* the package fails its structural checks or the official DTDs;
* the audio is simulated (mock provider): there is nothing to listen to.

Warnings that a person can resolve by looking (unaccounted source words,
low-confidence OCR, leftover hyphenation) hold the book at ``NEEDS_REVIEW``
until someone acknowledges them; the acknowledgement is recorded with its
time. ``RELEASED`` is an explicit act on a ``PASSED`` book.

The report keeps separate what is often lumped together: whether generation
technically worked, whether the source is complete, what the automatic content
checks found, whether the audio is sound, what people have reviewed, whether
the package is valid, and what nobody has verified. Nothing here can say the
book is error-free; ``PASSED`` means every *automated* check passed and every
flagged doubt was resolved, which is a measured result, not a guarantee.
"""

from __future__ import annotations

PENDING = "PENDING"
EXTRACTING = "EXTRACTING"
GENERATING = "GENERATING"
VALIDATING = "VALIDATING"
NEEDS_REVIEW = "NEEDS_REVIEW"
BLOCKED = "BLOCKED"
PASSED = "PASSED"
RELEASED = "RELEASED"

LABELS = {
    PENDING: "Pendiente", EXTRACTING: "Extrayendo texto", GENERATING: "Narrando", VALIDATING: "Validando",
    NEEDS_REVIEW: "Requiere revisión", BLOCKED: "Bloqueado", PASSED: "Validado", RELEASED: "Publicado",
}

_ACKNOWLEDGEABLE = {"unaccounted_words", "ocr_low_confidence", "broken_word", "suspicious_token",
                    "encoding_damage", "text_in_image", "image_unchecked", "outside_chapter", "epub_footer"}


def _issue_id(issue: dict) -> str:
    return f"{issue.get('code')}:{issue.get('location', '')}:{issue.get('text', '')[:40]}"


def evaluate(job: dict, *, extraction: dict | None, completeness: dict | None, validation: dict | None,
             dtd: dict | None, simulated_audio: bool, acknowledgements: list[dict] | None = None,
             released: dict | None = None) -> dict:
    acknowledged = {item.get("id") for item in acknowledgements or []}
    units = job.get("sentences", [])
    counts: dict[str, int] = {}
    for unit in units:
        counts[unit["state"]] = counts.get(unit["state"], 0) + 1
    blockers: list[str] = []
    reviews: list[str] = []

    # --- Technical generation --------------------------------------------- #
    job_state = job.get("state", "")
    failed_units = counts.get("failed", 0)
    pending_units = counts.get("pending", 0) + counts.get("generating", 0)
    awaiting = counts.get("needs_human_review", 0) + counts.get("unresolved", 0)
    technical = {
        "status": "failed" if failed_units else ("passed" if job_state == "completed" else "in_progress"),
        "segments": len(units), "verified": counts.get("verified", 0), "failed": failed_units,
        "pending": pending_units, "awaiting_review": awaiting,
        "technical_attempts": sum(int(unit.get("technical_attempts") or 0) for unit in units),
        "quality_retakes": sum(int(unit.get("quality_retakes") or 0) for unit in units),
        "reused_from_cache": sum(1 for unit in units if unit.get("reused_audio")),
    }
    if failed_units:
        blockers.append(f"{failed_units} segmentos fallaron técnicamente y no tienen audio.")

    # --- Source completeness ---------------------------------------------- #
    extraction = extraction or {}
    issues = extraction.get("issues") or []
    blocking_issues = [issue for issue in issues if issue.get("severity") == "blocking"]
    open_warnings = [issue for issue in issues if issue.get("severity") == "warning"
                     and _issue_id(issue) not in acknowledged]
    for issue in blocking_issues:
        blockers.append(f"Extracción: {issue.get('message')}")
    for issue in open_warnings:
        reviews.append(f"Extracción: {issue.get('message')}")
    source = {
        "status": "failed" if blocking_issues else ("needs_review" if open_warnings else
                                                    ("passed" if extraction else "not_run")),
        "method": extraction.get("method"), "coverage": extraction.get("coverage", {}),
        "blocking": blocking_issues, "open_warnings": [{**issue, "id": _issue_id(issue)} for issue in open_warnings],
        "acknowledged": len([issue for issue in issues if _issue_id(issue) in acknowledged]),
        "pages": len(extraction.get("pages") or []),
        "ocr_pages": sum(1 for page in extraction.get("pages") or [] if page.get("method") == "ocr"),
    }

    # --- Automated content verification ----------------------------------- #
    verifications: dict[str, int] = {}
    for unit in units:
        if unit["state"] == "verified":
            key = unit.get("verification") or ("auto_accepted" if str(unit.get("last_error", "")).startswith(
                "Aceptada automáticamente") else "human" if "human reviewer" in str(unit.get("last_error", ""))
                else "asr_primary")
            verifications[key] = verifications.get(key, 0) + 1
    metrics = [unit.get("metrics") or {} for unit in units if unit.get("metrics")]
    words = sum(int(item.get("reference_words") or 0) for item in metrics)
    errors = sum(int(item.get("substitutions") or 0) + int(item.get("deletions") or 0)
                 + int(item.get("insertions") or 0) for item in metrics)
    content = {
        "status": "simulated" if simulated_audio else ("needs_review" if awaiting else
                                                      ("passed" if units and counts.get("verified") == len(units)
                                                       else "in_progress")),
        "verified_by": verifications,
        "awaiting_review": awaiting,
        "reference_words": words,
        "asr_word_errors": errors,
        "wer": round(errors / words, 4) if words else None,
        "segments_measured": len(metrics),
    }
    if awaiting:
        reviews.append(f"{awaiting} segmentos esperan una decisión humana.")
    if pending_units:
        reviews.append(f"{pending_units} segmentos aún no se han narrado.")

    # --- Audio integrity --------------------------------------------------- #
    integrity_failed = [unit["sentence_id"] for unit in units
                        if (unit.get("acoustic") or {}).get("status") == "failed" and unit["state"] == "verified"]
    unchecked = [unit["sentence_id"] for unit in units
                 if (unit.get("acoustic") or {}).get("status") in {"unavailable", "skipped", "not_performed", None}]
    audio = {
        "status": "simulated" if simulated_audio else ("failed" if integrity_failed else
                                                       ("partial" if unchecked else "passed")),
        "failed": integrity_failed, "signal_unchecked": len(unchecked),
        "suspicious": sum(1 for unit in units if (unit.get("acoustic") or {}).get("status") == "needs_review"),
    }
    if simulated_audio:
        blockers.append("El audio es simulado (proveedor de prueba): no hay nada que escuchar ni publicar.")
    if integrity_failed:
        blockers.append(f"{len(integrity_failed)} segmentos tienen audio defectuoso.")
    if unchecked and not simulated_audio:
        reviews.append(f"{len(unchecked)} segmentos no pudieron analizarse acústicamente (falta ffmpeg).")

    # --- Completeness and package ----------------------------------------- #
    completeness = completeness or {}
    for check in completeness.get("checks") or []:
        if check.get("status") == "failed":
            blockers.append(f"{check.get('label')}: {check.get('detail')}")
    validation = validation or {}
    if validation.get("status") == "failed":
        blockers.append(f"Validación del paquete: {validation.get('detail', 'fallida')}")
    dtd = dtd or {}
    if dtd.get("status") == "failed":
        blockers.append(f"El paquete no cumple las DTD de Z39.86-2005 ({len(dtd.get('errors') or [])} errores).")
    package = {
        "status": ("failed" if validation.get("status") == "failed" or dtd.get("status") == "failed"
                   or completeness.get("status") == "failed"
                   else "passed" if validation.get("status") == "passed" and dtd.get("status") == "passed"
                   and completeness.get("status") == "passed" else "not_run"),
        "structural": validation.get("status", "not_run"), "dtd": dtd.get("status", "not_run"),
        "completeness": completeness.get("status", "not_run"),
        "official_daisy_pipeline": "not_run",
    }

    # --- Human review ------------------------------------------------------ #
    flagged = [unit for unit in units if unit["state"] == "verified"
               and (unit.get("diagnosis") or {}).get("action") == "accept_flagged"]
    human = {
        "decisions": int(job.get("human_decisions") or 0),
        "awaiting": awaiting,
        "acknowledged_warnings": len(acknowledged),
        "listening_recommended": [unit["sentence_id"] for unit in flagged][:200],
        "listening_check": "not_recorded",
    }

    # --- State ------------------------------------------------------------- #
    if job_state in {"queued", ""} and not any(unit.get("technical_attempts") for unit in units):
        state = PENDING
    elif job_state in {"queued", "running"}:
        state = GENERATING
    elif job_state == "assembling":
        state = VALIDATING
    elif blockers or job_state == "failed":
        state = BLOCKED
    elif reviews or job_state != "completed":
        state = NEEDS_REVIEW
    else:
        state = PASSED
    if job_state == "cancelled" and state not in {BLOCKED}:
        state = BLOCKED
        blockers.append("El trabajo se detuvo antes de terminar.")
    if state == PASSED and released:
        state = RELEASED

    uncertainty = [
        "Un reconocimiento automático que coincide con el texto no demuestra que la pronunciación, la "
        "entonación o el acento sean correctos.",
        "Las métricas de error (WER) miden el acuerdo con un reconocedor, que también se equivoca.",
        "La validación DTD y las comprobaciones propias no sustituyen al validador DAISY Pipeline ni a la "
        "prueba en un reproductor DAISY real.",
    ]
    if flagged:
        uncertainty.append(f"{len(flagged)} segmentos se aceptaron con dudas del reconocedor; conviene escucharlos.")
    if not human["decisions"]:
        uncertainty.append("Ninguna persona ha registrado una escucha del libro.")
    return {
        "state": state, "label": LABELS[state], "blockers": blockers, "reviews": reviews,
        "dimensions": {"technical": technical, "source": source, "content": content, "audio": audio,
                       "package": package, "human": human},
        "uncertainty": uncertainty, "released": released or None,
        "releasable": state == PASSED,
    }


def acknowledgeable(issue_code: str) -> bool:
    return issue_code in _ACKNOWLEDGEABLE
