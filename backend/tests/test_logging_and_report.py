"""The readable job log and the quality report, including unhappy outcomes."""

from __future__ import annotations

import json

from app import quality_report
from app.job_logging import JobLogger, redact


# --------------------------------------------------------------------------- #
# job.log
# --------------------------------------------------------------------------- #

def test_log_lines_are_readable_and_carry_the_job_scope(tmp_path) -> None:
    log = JobLogger(tmp_path, "1a2b3c4d-5555-6666-7777-888899990000", book_id="abcdef1234567890")
    log.info("tts.synthesize", "sentence audio generated", chapter_index=2, sentence_id="s-0007abcdef12",
             provider="deepgram", attempt=1, reused=False, bytes=34848)

    line = (tmp_path / "job.log").read_text(encoding="utf-8").strip()
    assert not line.startswith("{")
    assert " INFO " in line
    assert "job=1a2b3c4d" in line
    assert "book=abcdef123456" in line
    assert "ch=003" in line
    assert "sentence=s-0007abcdef" in line
    assert "tts.synthesize: sentence audio generated" in line
    assert "provider=deepgram" in line
    assert "attempt=1" in line
    assert "reused=no" in line


def test_levels_are_recorded_for_warnings_and_errors(tmp_path) -> None:
    log = JobLogger(tmp_path, "job-1")
    log.warning("qa.retake", "quality retake scheduled", quality_retake=1)
    log.error("tts.synthesize", "sentence synthesis failed", error="credit (402): sin saldo")

    text = (tmp_path / "job.log").read_text(encoding="utf-8")
    assert " WARNING " in text
    assert " ERROR " in text
    assert "credit (402)" in text


def test_structured_events_are_kept_beside_the_readable_log(tmp_path) -> None:
    log = JobLogger(tmp_path, "job-1", book_id="book-1")
    log.info("job.start", "production started", total_sentences=42)

    record = json.loads((tmp_path / "job-events.jsonl").read_text(encoding="utf-8").strip())
    assert record["operation"] == "job.start"
    assert record["message"] == "production started"
    assert record["total_sentences"] == 42
    assert record["job_id"] == "job-1"
    assert record["book_id"] == "book-1"
    assert record["level"] == "INFO"
    assert record["timestamp"].endswith("Z")


def test_credentials_are_redacted_if_they_ever_reach_a_log_field(tmp_path) -> None:
    assert "<redacted>" in redact("Authorization: Bearer sk_live_abcdef1234567890")
    assert "<redacted>" in redact("key=0123456789abcdef0123456789abcdef")

    log = JobLogger(tmp_path, "job-1")
    log.error("tts.synthesize", "failed", error="Bearer sk_live_abcdef1234567890 rejected")
    text = (tmp_path / "job.log").read_text(encoding="utf-8")
    assert "sk_live_abcdef1234567890" not in text
    assert "<redacted>" in text


def test_long_field_values_are_truncated_rather_than_flooding_the_log(tmp_path) -> None:
    log = JobLogger(tmp_path, "job-1")
    log.info("qa.compare", "done", detail="x" * 5000)
    line = (tmp_path / "job.log").read_text(encoding="utf-8")
    assert len(line) < 1000
    assert "..." in line


# --------------------------------------------------------------------------- #
# Quality report
# --------------------------------------------------------------------------- #

def _sentence(sentence_id: str, state: str, **overrides) -> dict:
    row = {
        "sentence_id": sentence_id, "chapter_index": 0, "chapter_title": "Capítulo 1", "sequence": 0,
        "state": state, "text": "La casa tenía 147 ventanas.", "recognized_text": "La casa tenía 1 4 7 ventanas.",
        "issues": [], "confidence": 1.0, "technical_attempts": 1, "quality_retakes": 0,
        "audio_path": "audio/chapter-001/segment-0001.mp3", "acoustic": {"status": "passed"},
        "last_error": "", "reused_audio": 0, "updated_at": "2026-10-20T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def _job(sentences: list[dict], state: str = "partially_completed") -> dict:
    return {
        "job_id": "job-1", "source_name": "libro.txt", "state": state,
        "chapters": [{"title": "Capítulo 1", "paragraphs": ["La casa tenía 147 ventanas."]}],
        "metadata": {"title": "La casa junto al río", "author": "Marta Olivares", "edition": "1.ª",
                     "publication_year": "2021", "publisher": "Editorial Candil"},
        "voice_id": "aura-2-diana-es", "voice_speed": 1.0, "sentences": sentences,
    }


def test_report_counts_each_outcome_separately() -> None:
    job = _job([
        _sentence("a", "verified"),
        _sentence("b", "verified", quality_retakes=2),
        _sentence("c", "needs_human_review", confidence=0.6, issues=[{
            "kind": "delete", "category": "ambiguous_expression", "difference_type": "omission",
            "expected": "y", "recognized": "", "reason": "Palabra funcional corta.",
        }]),
        _sentence("d", "failed", audio_path=None, last_error="credit (402): sin saldo"),
    ])
    report = quality_report.build_report(job, [], provider="deepgram", asr_provider="deepgram",
                                         voice_name="Diana", voice_language="es-ES")
    totals = report["totals"]
    assert totals["correct"] == 1
    assert totals["corrected"] == 1      # verified after a retake
    assert totals["requires_review"] == 1
    assert totals["failed"] == 1
    assert totals["quality_retakes"] == 2


def test_human_decisions_are_recorded_in_the_report() -> None:
    events = [{
        "timestamp": "2026-10-20T10:00:00+00:00", "level": "INFO", "message": "human review decision",
        "details": {"sentence_id": "c", "decision": "correct", "corrected_text": "Texto corregido."},
    }]
    report = quality_report.build_report(_job([_sentence("c", "verified")]), events)
    assert report["human_decisions"][0]["decision"] == "correct"
    assert report["human_decisions"][0]["corrected_text"] == "Texto corregido."
    # A sentence a person corrected is reported as corrected, not simply correct.
    assert report["totals"]["corrected"] == 1
    assert report["totals"]["correct"] == 0


def test_report_never_invents_the_unmeasured_metrics() -> None:
    report = quality_report.build_report(_job([_sentence("a", "verified")]), [])
    assert all(value is None for name, value in report["metrics"].items() if name != "note")
    assert "no se han medido" in report["metrics"]["note"]


def test_html_report_is_self_contained_and_shows_every_outcome() -> None:
    job = _job([
        _sentence("a", "verified"),
        _sentence("c", "needs_human_review", issues=[{
            "kind": "replace", "category": "ambiguous_expression", "difference_type": "substitution",
            "expected": "días", "recognized": "día", "reason": "Palabras casi homófonas.",
        }]),
        _sentence("d", "failed", audio_path=None, last_error="credit (402): la cuenta no tiene saldo"),
    ])
    html = quality_report.render_html(
        quality_report.build_report(job, [], provider="deepgram", asr_provider="deepgram",
                                    voice_name="Diana", voice_language="es-ES",
                                    validation={"status": "passed", "official_daisy_pipeline": "not_run"},
                                    cache_stats={"hits": 3, "misses": 1}),
        log_excerpt="2026-10-20T10:00:00.000Z INFO    [job=job-1] job.start: production started",
    )
    assert html.startswith("<!doctype html>")
    assert "<style>" in html and "<script>" in html
    # No external resources, so it opens from the delivery ZIP offline.
    assert 'src="http' not in html and 'href="http' not in html
    assert "Diana" in html and "es-ES" in html
    assert "Requiere revisión humana" in html
    assert "Fallo técnico" in html
    assert "Palabras casi homófonas." in html
    assert "la cuenta no tiene saldo" in html
    assert "production started" in html
    assert 'data-outcome="review"' in html
    assert 'data-outcome="failed"' in html


def test_html_report_escapes_book_text_rather_than_rendering_it() -> None:
    job = _job([_sentence("a", "verified", text='<script>alert("x")</script>',
                          recognized_text="texto reconocido")])
    html = quality_report.render_html(quality_report.build_report(job, []))
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert" in html


def test_html_report_groups_sentences_by_chapter() -> None:
    job = _job([
        _sentence("a", "verified", chapter_index=0, chapter_title="Capítulo 1"),
        _sentence("b", "verified", chapter_index=1, chapter_title="Capítulo 2"),
    ])
    job["chapters"].append({"title": "Capítulo 2", "paragraphs": ["Texto."]})
    html = quality_report.render_html(quality_report.build_report(job, []))
    assert "Capítulo 1: Capítulo 1" in html
    assert "Capítulo 2: Capítulo 2" in html
    assert html.count('class="chapter card"') == 2


def test_report_states_when_the_daisy_package_has_not_been_built() -> None:
    report = quality_report.build_report(_job([_sentence("a", "verified")]), [])
    assert report["validation"] == {"status": "not_run"}
    html = quality_report.render_html(report)
    # Said in words, not as the raw internal value.
    assert "No ejecutada" in html
    assert ">not_run<" not in html


def test_html_report_opens_with_a_plain_verdict() -> None:
    ready = quality_report.render_html(quality_report.build_report(
        _job([_sentence("a", "verified"), _sentence("b", "verified")], state="completed"), []))
    assert "Listo para entregar" in ready
    assert "No hay nada pendiente" in ready
    assert "Completado" in ready and "completed<" not in ready

    flagged = quality_report.render_html(quality_report.build_report(
        _job([_sentence("a", "verified"), _sentence("b", "needs_human_review",
                                                     recognized_text="La casa tenía ventanas.")]), []))
    assert "1 frase necesita tu revisión" in flagged
    assert "Pendiente de revisión" in flagged and "partially_completed" not in flagged
    # The flagged sentence is listed up front, with what was actually heard.
    attention = flagged.split('<div class="attn">', 1)[1].split("<h2>Todas las frases", 1)[0]
    assert 'data-outcome="review"' in attention and "Se oye:" in attention
    # A chapter with something to review starts open.
    assert '<details class="chapter card" open>' in flagged

    failed = quality_report.render_html(quality_report.build_report(
        _job([_sentence("d", "failed", audio_path=None)]), []))
    assert "La producción tiene fallos" in failed


def test_passages_accepted_automatically_are_their_own_outcome() -> None:
    minor = [{"kind": "replace", "category": "ambiguous_expression", "difference_type": "substitution",
              "expected": "Buenos", "recognized": "Buenas", "reason": "Palabras casi homófonas."}]
    job = _job([
        _sentence("a", "verified"),
        _sentence("b", "verified", issues=minor,
                  last_error="Aceptada automáticamente: diferencias menores que el reconocimiento automático suele cometer."),
    ], state="completed")
    report = quality_report.build_report(job, [])
    assert report["totals"]["auto_accepted"] == 1
    assert report["totals"]["correct"] == 1
    html = quality_report.render_html(report)
    assert 'data-filter="auto"' in html
    assert 'data-outcome="auto"' in html
    # Accepted, so the book is ready; the report still says what was let through.
    assert "Listo para entregar" in html
    assert "se aceptaron automáticamente" in html
    attention = html.split('<div class="attn">', 1)[1].split("<h2>Todas las frases", 1)[0]
    assert 'data-outcome="auto"' not in attention
    assert "No hay nada pendiente" in attention
