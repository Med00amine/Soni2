"""Build the machine-readable and human-readable quality reports for a job.

The HTML report is self-contained: no external stylesheet, script or font, so
it opens from the delivery ZIP with no network access. It reports what the job
actually recorded, including the explicit "correct after normalization" and
"requires human review" outcomes, so a normalized numeric difference is never
presented as an error.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any, Iterable

from app import jobs as job_store
from app.qa_review import AUTO_ACCEPT_PREFIX

# Sentence states mapped onto the outcomes a reviewer needs to tell apart.
_OUTCOMES: dict[str, tuple[str, str, str]] = {
    "verified": ("correct", "Correcta", "Coincide con el texto tras la normalización en español."),
    "corrected": ("corrected", "Corregida y revalidada", "Se regeneró o corrigió y después superó la comparación."),
    "auto": ("auto", "Aceptada automáticamente",
             "Solo quedaban diferencias menores, de las que comete el reconocimiento automático; "
             "se aceptó sin revisión humana."),
    "needs_human_review": ("review", "Requiere revisión humana", "Diferencia ambigua o revisión automática no disponible."),
    "unresolved": ("unresolved", "Sin resolver", "Sigue pendiente después de la revisión."),
    "failed": ("failed", "Fallo técnico", "La generación de audio no se completó."),
    "pending": ("pending", "Pendiente", "Todavía no se ha generado."),
    "generating": ("pending", "En curso", "Generación en marcha."),
}


def _outcome(sentence: dict[str, Any], *, human_corrected: set[str]) -> tuple[str, str, str]:
    state = sentence.get("state", "pending")
    if state == "verified" and str(sentence.get("last_error") or "").startswith(AUTO_ACCEPT_PREFIX):
        return _OUTCOMES["auto"]
    if state == "verified" and (sentence.get("quality_retakes") or sentence["sentence_id"] in human_corrected):
        return _OUTCOMES["corrected"]
    return _OUTCOMES.get(state, ("pending", state, ""))


def build_report(
    job: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    provider: str = "",
    asr_provider: str = "",
    voice_name: str = "",
    voice_language: str = "",
    validation: dict[str, Any] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the JSON quality report from real job state."""
    sentences = job["sentences"]
    human_corrected = {
        event.get("details", {}).get("sentence_id")
        for event in events
        if event.get("message") == "human review decision"
        and event.get("details", {}).get("decision") in {"correct", "retry"}
    }
    human_decisions = [
        {
            "sentence_id": event.get("details", {}).get("sentence_id"),
            "decision": event.get("details", {}).get("decision"),
            "corrected_text": event.get("details", {}).get("corrected_text"),
            "timestamp": event.get("timestamp"),
        }
        for event in events
        if event.get("message") == "human review decision"
    ]

    rows: list[dict[str, Any]] = []
    counters: dict[str, int] = {}
    for sentence in sentences:
        key, label, explanation = _outcome(sentence, human_corrected=human_corrected)
        counters[key] = counters.get(key, 0) + 1
        rows.append({
            "sentence_id": sentence["sentence_id"],
            "chapter_index": sentence["chapter_index"],
            "chapter_title": sentence.get("chapter_title", ""),
            "sequence": sentence["sequence"],
            "state": sentence["state"],
            "outcome": key,
            "outcome_label": label,
            "outcome_explanation": explanation,
            "expected_text": sentence["text"],
            "recognized_text": sentence["recognized_text"],
            "issues": sentence["issues"],
            "confidence": sentence["confidence"],
            "technical_attempts": sentence["technical_attempts"],
            "quality_retakes": sentence["quality_retakes"],
            "audio_reference": sentence["audio_path"],
            "acoustic_checks": sentence["acoustic"],
            "last_error": sentence.get("last_error", ""),
            "reused_audio": bool(sentence.get("reused_audio")),
        })

    metadata = job.get("metadata") or {}
    return {
        "job_id": job["job_id"],
        "source_name": job["source_name"],
        "state": job["state"],
        "generated_at": job_store.utc_now(),
        "book": {
            "title": metadata.get("title", ""),
            "author": metadata.get("author", ""),
            "edition": metadata.get("edition", ""),
            "publication_year": metadata.get("publication_year", ""),
            "publisher": metadata.get("publisher", ""),
        },
        "narration": {
            "tts_provider": provider,
            "asr_provider": asr_provider,
            "voice_id": job.get("voice_id", ""),
            "voice_name": voice_name,
            "voice_language": voice_language,
            "voice_speed": job.get("voice_speed", 1.0),
        },
        "totals": {
            "chapters": len(job.get("chapters") or []),
            "sentences": len(sentences),
            "correct": counters.get("correct", 0),
            "corrected": counters.get("corrected", 0),
            "auto_accepted": counters.get("auto", 0),
            "requires_review": counters.get("review", 0),
            "unresolved": counters.get("unresolved", 0),
            "failed": counters.get("failed", 0),
            "pending": counters.get("pending", 0),
            "technical_attempts": sum(item["technical_attempts"] for item in sentences),
            "quality_retakes": sum(item["quality_retakes"] for item in sentences),
            "reused_audio": sum(1 for item in sentences if item.get("reused_audio")),
        },
        "audio_cache": cache_stats or {},
        "validation": validation or {"status": "not_run"},
        "human_decisions": human_decisions,
        "sentences": rows,
        "events": events,
        "acoustic_analysis": "Las comprobaciones de ffmpeg (duración, silencio y saturación) son indicativas; "
                             "no constituyen una medida validada de calidad de voz.",
        "metrics": {
            "note": "Estas métricas requieren un corpus anotado de forma independiente; no se han medido.",
            "real_error_detection": None,
            "false_alarms_per_audio_hour": None,
            "human_review_minutes_per_audio_hour": None,
            "automatic_correction_rate": None,
        },
    }


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


_STYLE = """
:root{color-scheme:light dark;--bg:#f4f6f8;--panel:#fff;--ink:#14181d;--muted:#5d6672;--line:#e1e5ea;
--accent:#1f5fbf;--ok:#167340;--ok-bg:#e5f4eb;--warn:#8a5800;--warn-bg:#fdf2d8;--bad:#a8281e;--bad-bg:#fbe8e6;
--info:#1f5fbf;--info-bg:#e7effc;--mute-bg:#eef1f4;--shadow:0 1px 2px rgba(16,24,40,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#121519;--panel:#1b1f25;--ink:#e8ebf0;--muted:#a1abb8;
--line:#2c323a;--accent:#7fb0ff;--ok:#7fd4a2;--ok-bg:#122e1e;--warn:#f0c674;--warn-bg:#33270f;--bad:#ff9d93;
--bad-bg:#3a1916;--info:#7fb0ff;--info-bg:#15233a;--mute-bg:#232830;--shadow:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:32px 16px 72px}
.eyebrow{margin:0;font-size:.78rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
h1{font-size:1.9rem;line-height:1.2;margin:4px 0 6px}
.by{margin:0 0 24px;color:var(--muted)}
h2{font-size:1.2rem;margin:40px 0 14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.verdict{display:flex;gap:16px;align-items:flex-start;padding:20px;border-radius:14px;border:1px solid transparent}
.verdict .icon{flex:none;width:44px;height:44px;border-radius:50%;display:grid;place-items:center;font-size:1.4rem;
font-weight:800;color:#fff}
.verdict h2{margin:0 0 4px;font-size:1.25rem}
.verdict p{margin:0}
.v-ok{background:var(--ok-bg);border-color:var(--ok)}.v-ok .icon{background:var(--ok)}.v-ok h2{color:var(--ok)}
.v-warn{background:var(--warn-bg);border-color:var(--warn)}.v-warn .icon{background:var(--warn)}.v-warn h2{color:var(--warn)}
.v-bad{background:var(--bad-bg);border-color:var(--bad)}.v-bad .icon{background:var(--bad)}.v-bad h2{color:var(--bad)}
@media (prefers-color-scheme:dark){.verdict .icon{color:#121519}}
.bar{display:flex;height:12px;border-radius:999px;overflow:hidden;background:var(--mute-bg);margin:22px 0 10px}
.bar span{display:block;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;list-style:none;margin:0;padding:0;font-size:.92rem}
.legend li{display:flex;align-items:center;gap:7px}
.dot{width:10px;height:10px;border-radius:50%;flex:none}
.c-correct{background:var(--ok)}.c-corrected{background:var(--info)}.c-auto{background:var(--ok);opacity:.55}
.c-review{background:var(--warn)}
.c-unresolved,.c-failed{background:var(--bad)}.c-pending{background:var(--muted)}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:1px;margin:22px 0 0;
background:var(--line);border-radius:14px;overflow:hidden;border:1px solid var(--line)}
.facts div{background:var(--panel);padding:14px 16px;min-width:0}
.facts dt{font-size:.78rem;color:var(--muted);margin:0 0 2px}
.facts dd{margin:0;font-weight:650;overflow-wrap:anywhere}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:.78rem;font-weight:700;white-space:nowrap}
.b-correct{background:var(--ok-bg);color:var(--ok)}.b-corrected{background:var(--info-bg);color:var(--info)}
.b-auto{background:var(--ok-bg);color:var(--ok);border:1px dashed var(--ok)}
.b-review{background:var(--warn-bg);color:var(--warn)}.b-unresolved,.b-failed{background:var(--bad-bg);color:var(--bad)}
.b-pending{background:var(--mute-bg);color:var(--muted)}
.hint{color:var(--muted);font-size:.92rem;margin:-6px 0 14px}
.calm{padding:16px 18px;color:var(--muted)}
.s{padding:14px 16px;border-top:1px solid var(--line)}
.chapter .s:first-of-type{border-top:0}
.attn .s{border:1px solid var(--line);border-left:4px solid var(--warn);border-radius:12px;margin-bottom:10px;
background:var(--panel);box-shadow:var(--shadow)}
.attn .s[data-outcome=failed],.attn .s[data-outcome=unresolved]{border-left-color:var(--bad)}
.s-top{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.s-where{font-size:.8rem;color:var(--muted)}
.s-text{margin:6px 0 0;font-size:1.02rem;overflow-wrap:anywhere}
.heard{margin:6px 0 0;font-size:.94rem;color:var(--muted);overflow-wrap:anywhere}
.heard strong{color:var(--ink);font-weight:600}
.why{margin:6px 0 0;font-size:.92rem}
ul.issues{margin:8px 0 0;padding-left:20px;font-size:.92rem}
ul.issues li{margin-bottom:4px}
audio{display:block;width:100%;max-width:420px;height:36px;margin-top:10px}
.k{font-size:.78rem;color:var(--muted);margin-top:8px;overflow-wrap:anywhere}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:14px}
.tools button{font:inherit;font-size:.88rem;padding:6px 14px;border:1px solid var(--line);background:var(--panel);
color:var(--ink);border-radius:999px;cursor:pointer}
.tools button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
@media (prefers-color-scheme:dark){.tools button[aria-pressed=true]{color:#121519}}
.tools input{font:inherit;font-size:.92rem;padding:8px 12px;border:1px solid var(--line);border-radius:10px;
background:var(--panel);color:var(--ink);flex:1 1 220px;min-width:0}
.shown{font-size:.85rem;color:var(--muted)}
details.chapter{margin-bottom:10px;overflow:hidden}
details.chapter>summary{list-style:none;cursor:pointer;padding:14px 16px;display:flex;gap:12px;align-items:center;
justify-content:space-between;flex-wrap:wrap}
details.chapter>summary::-webkit-details-marker{display:none}
details.chapter>summary h3{margin:0;font-size:1rem;display:flex;gap:10px;align-items:center}
details.chapter>summary h3::before{content:"\\25B8";color:var(--muted);transition:transform .15s}
details.chapter[open]>summary h3::before{transform:rotate(90deg)}
details.chapter[open]>summary{border-bottom:1px solid var(--line)}
.mini{display:flex;gap:6px;flex-wrap:wrap}
details.tech{margin-top:40px}
details.tech>summary{cursor:pointer;padding:14px 16px;font-weight:650}
.tech-body{padding:0 16px 16px}
.tech-body h3{font-size:.95rem;margin:22px 0 8px}
table{width:100%;border-collapse:collapse;font-size:.88rem}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;width:38%}
thead th{width:auto}
.tw{overflow-x:auto}
pre{overflow-x:auto;background:var(--mute-bg);padding:12px;border-radius:10px;font-size:.8rem;max-height:420px}
.note{font-size:.88rem;color:var(--muted)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.8rem}
@media print{.tools{display:none}}
"""

_SCRIPT = """
(function(){
  var cards=Array.prototype.slice.call(document.querySelectorAll('.chapter .s'));
  var chapters=Array.prototype.slice.call(document.querySelectorAll('details.chapter'));
  var buttons=Array.prototype.slice.call(document.querySelectorAll('.tools button'));
  var search=document.getElementById('q');
  var count=document.getElementById('shown');
  var active='all';
  function apply(){
    var needle=(search.value||'').toLowerCase();
    var shown=0;
    cards.forEach(function(card){
      var byState=active==='all'||card.getAttribute('data-outcome')===active;
      var byText=!needle||card.textContent.toLowerCase().indexOf(needle)>-1;
      card.hidden=!(byState&&byText);
      if(!card.hidden){shown++;}
    });
    var filtering=active!=='all'||needle;
    chapters.forEach(function(block){
      var any=Array.prototype.some.call(block.querySelectorAll('.s'),function(c){return !c.hidden;});
      block.hidden=!any;
      if(filtering&&any){block.open=true;}
    });
    count.textContent='Mostrando '+shown+' de '+cards.length+' frases';
  }
  buttons.forEach(function(button){
    button.addEventListener('click',function(){
      active=button.getAttribute('data-filter');
      buttons.forEach(function(other){other.setAttribute('aria-pressed',String(other===button));});
      apply();
    });
  });
  search.addEventListener('input',apply);
  apply();
})();
"""

# Plain-language labels, so the report never shows a raw internal value.
_JOB_STATES = {
    "completed": "Completado", "partially_completed": "Pendiente de revisión", "failed": "Fallido",
    "running": "En curso", "queued": "En cola", "cancelled": "Cancelado",
}
_VALIDATION_LABELS = {
    "status": "Resultado", "method": "Método", "checks": "Comprobaciones", "detail": "Detalle",
    "audio_joined_with_ffmpeg": "Audio unido con ffmpeg", "official_daisy_pipeline": "Validador oficial DAISY Pipeline",
    "note": "Nota",
}
_VALIDATION_VALUES = {
    "passed": "Superada", "failed": "Fallida", "not_run": "No ejecutada", "unknown": "Desconocida",
    "soni2_structural_validation": "Validación estructural de Soni2", "True": "Sí", "False": "No",
}
_DECISIONS = {"accept": "Aceptada tal cual", "retry": "Volver a narrar", "correct": "Texto corregido"}
_METRIC_LABELS = {
    "real_error_detection": "Detección de errores reales",
    "false_alarms_per_audio_hour": "Falsas alarmas por hora de audio",
    "human_review_minutes_per_audio_hour": "Minutos de revisión humana por hora de audio",
    "automatic_correction_rate": "Tasa de corrección automática",
}
_CACHE_LABELS = {"hits": "Frases reutilizadas de la caché", "misses": "Frases generadas de nuevo"}
_OUTCOME_ORDER = (
    ("correct", "correct", "Correctas"),
    ("corrected", "corrected", "Corregidas y revalidadas"),
    ("auto", "auto_accepted", "Aceptadas automáticamente"),
    ("review", "requires_review", "Requieren revisión"),
    ("unresolved", "unresolved", "Sin resolver"),
    ("failed", "failed", "Fallos técnicos"),
    ("pending", "pending", "Pendientes"),
)


def _issue_items(issues: Iterable[dict[str, Any]]) -> str:
    labels = {
        "ambiguous_expression": "Expresión ambigua",
        "likely_mismatch": "Diferencia probable",
        "audio_defect": "Posible defecto de audio",
    }
    types = {
        "omission": "falta en el audio",
        "addition_or_repetition": "sobra o se repite en el audio",
        "substitution": "se oye otra palabra",
        "audio_defect": "defecto de audio",
    }
    items = []
    for issue in issues:
        label = labels.get(str(issue.get("category", "")), "Diferencia")
        kind = types.get(str(issue.get("difference_type", "")), str(issue.get("kind", "")))
        parts = [f"<strong>{_e(label)}</strong>: {_e(kind)}"]
        if issue.get("expected"):
            parts.append(f"en el texto «{_e(issue['expected'])}»")
        if issue.get("recognized"):
            parts.append(f"se oye «{_e(issue['recognized'])}»")
        if issue.get("reason"):
            parts.append(_e(issue["reason"]))
        items.append("<li>" + " · ".join(parts) + "</li>")
    return f"<ul class=\"issues\">{''.join(items)}</ul>" if items else ""


def _friendly_time(value: object) -> str:
    """2026-10-22T13:27:33+00:00 -> 22/10/2026, 13:27 (UTC)."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value or "—")
    return moment.strftime("%d/%m/%Y, %H:%M") + (" (UTC)" if moment.utcoffset() is not None else "")


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _verdict(report: dict[str, Any]) -> tuple[str, str, str, str]:
    """(css class, icon, headline, explanation) that a person can act on."""
    totals = report["totals"]
    good = totals["correct"] + totals["corrected"] + totals.get("auto_accepted", 0)
    attention = totals["requires_review"] + totals["unresolved"]
    if totals["failed"] or report["state"] == "failed":
        return ("v-bad", "×", "La producción tiene fallos",
                f"{_plural(totals['failed'], 'frase no tiene', 'frases no tienen')} audio porque la generación "
                "no se completó. Vuelve a intentarlo desde la aplicación; el resto del audio se conserva.")
    if attention:
        return ("v-warn", "!", f"{_plural(attention, 'frase necesita', 'frases necesitan')} tu revisión",
                f"{good} de {totals['sentences']} frases coinciden con el texto. Escucha las marcadas abajo y "
                "decide en la aplicación si se aceptan o se vuelven a narrar.")
    if totals["pending"]:
        return ("v-warn", "…", "La producción todavía no ha terminado",
                f"{_plural(totals['pending'], 'frase está pendiente', 'frases están pendientes')} de narrar.")
    notes = []
    if totals["corrected"]:
        notes.append(f"{totals['corrected']} se corrigieron por el camino")
    if totals.get("auto_accepted"):
        notes.append(f"{totals['auto_accepted']} tenían solo diferencias menores y se aceptaron automáticamente")
    tail = f"; {' y '.join(notes)}." if notes else "."
    return ("v-ok", "✓", "Listo para entregar",
            f"Las {totals['sentences']} frases se han narrado y coinciden con el texto{tail}")


def _sentence_card(row: dict[str, Any], audio_hrefs: dict[str, str], *, where: str = "") -> str:
    href = audio_hrefs.get(row["sentence_id"]) or row["audio_reference"]
    audio = (f'<audio controls preload="none" src="{_e(href)}" '
             f'aria-label="Escuchar la frase {row["sequence"] + 1}"></audio>' if href else "")
    # The transcript only helps when something is off. For a correct sentence
    # it differs only by normalization (147 / ciento cuarenta y siete) and is noise.
    heard = ""
    if row["outcome"] != "correct":
        recognized = row["recognized_text"] or ""
        heard = (f'<p class="heard">Se oye: <strong>{_e(recognized)}</strong></p>' if recognized
                 else '<p class="heard">Sin transcripción del audio.</p>')
    why = f'<p class="why">{_e(row["outcome_explanation"])}</p>' if row["outcome"] != "correct" else ""
    error = f'<p class="why">{_e(row["last_error"])}</p>' if row["last_error"] else ""
    confidence = row["confidence"]
    details = [
        f"intentos {row['technical_attempts']}", f"retomas {row['quality_retakes']}",
        f"confianza {confidence:.0%}" if isinstance(confidence, (int, float)) else "confianza —",
        "reutilizado de la caché" if row["reused_audio"] else "generado",
    ]
    acoustic = row.get("acoustic_checks") or {}
    if isinstance(acoustic, dict) and acoustic.get("status"):
        details.append(f"análisis acústico {acoustic['status']}")
    location = f"{_e(where)} · " if where else ""
    return (
        f'<article class="s" data-outcome="{_e(row["outcome"])}">'
        f'<div class="s-top"><span class="badge b-{_e(row["outcome"])}">{_e(row["outcome_label"])}</span>'
        f'<span class="s-where">{location}Frase {row["sequence"] + 1}</span></div>'
        f'<p class="s-text">{_e(row["expected_text"])}</p>{heard}{why}{_issue_items(row["issues"])}{error}'
        f'{audio}<div class="k">{_e(" · ".join(details))} · <span class="mono">{_e(row["sentence_id"])}</span></div>'
        f'</article>'
    )


def _table(rows: Iterable[tuple[str, object]]) -> str:
    body = "".join(f"<tr><th>{_e(name)}</th><td>{_e(value)}</td></tr>" for name, value in rows)
    return f'<div class="tw"><table><tbody>{body}</tbody></table></div>'


def render_html(report: dict[str, Any], *, audio_hrefs: dict[str, str] | None = None, log_excerpt: str = "") -> str:
    audio_hrefs = audio_hrefs or {}
    totals = report["totals"]
    book = report["book"]
    narration = report["narration"]
    validation = report["validation"]
    title = book["title"] or report["source_name"]

    css, icon, headline, explanation = _verdict(report)
    total = max(1, totals["sentences"])
    bar = "".join(
        f'<span class="c-{key}" style="width:{totals.get(field, 0) * 100 / total:.3f}%"></span>'
        for key, field, _label in _OUTCOME_ORDER if totals.get(field, 0)
    )
    legend = "".join(
        f'<li><span class="dot c-{key}"></span>{_e(label)}: <strong>{totals.get(field, 0)}</strong></li>'
        for key, field, label in _OUTCOME_ORDER if totals.get(field, 0) or key in ("correct", "review")
    )

    voice_label = narration["voice_name"] or narration["voice_id"] or "—"
    if narration["voice_language"]:
        voice_label = f"{voice_label} · {narration['voice_language']}"
    facts = (
        ("Capítulos", totals["chapters"]),
        ("Frases", totals["sentences"]),
        ("Voz", voice_label),
        ("Estado", _JOB_STATES.get(report["state"], report["state"])),
        ("Paquete DAISY", _VALIDATION_VALUES.get(str(validation.get("status")), validation.get("status", "—"))),
        ("Generado", _friendly_time(report["generated_at"])),
    )
    fact_list = "".join(f"<div><dt>{_e(label)}</dt><dd>{_e(value)}</dd></div>" for label, value in facts)

    chapters: dict[int, list[dict[str, Any]]] = {}
    for sentence in report["sentences"]:
        chapters.setdefault(sentence["chapter_index"], []).append(sentence)
    chapter_names = {index: items[0]["chapter_title"] or f"Capítulo {index + 1}" for index, items in chapters.items()}

    attention_rows = sorted(
        (row for row in report["sentences"] if row["outcome"] not in ("correct", "corrected", "auto")),
        key=lambda row: (row["chapter_index"], row["sequence"]),
    )
    attention = "".join(
        _sentence_card(row, audio_hrefs,
                       where=f"Capítulo {row['chapter_index'] + 1}: {chapter_names[row['chapter_index']]}")
        for row in attention_rows
    ) or '<div class="card calm">No hay nada pendiente: todas las frases están correctas.</div>'

    blocks: list[str] = []
    for chapter_index in sorted(chapters):
        items = sorted(chapters[chapter_index], key=lambda row: row["sequence"])
        counts: dict[tuple[str, str], int] = {}
        for row in items:
            key = (row["outcome"], row["outcome_label"])
            counts[key] = counts.get(key, 0) + 1
        mini = "".join(f'<span class="badge b-{_e(outcome)}">{count} {_e(label.lower())}</span>'
                       for (outcome, label), count in counts.items())
        # Chapters with something to look at start open; the rest stay folded.
        flagged = any(row["outcome"] not in ("correct", "corrected", "auto") for row in items)
        blocks.append(
            f'<details class="chapter card"{" open" if flagged else ""}><summary>'
            f'<h3>Capítulo {chapter_index + 1}: {_e(chapter_names[chapter_index])}</h3>'
            f'<span class="mini">{mini}</span></summary>'
            f'{"".join(_sentence_card(row, audio_hrefs) for row in items)}</details>'
        )

    decisions = report["human_decisions"]
    decision_block = (
        '<div class="tw"><table><thead><tr><th>Frase</th><th>Decisión</th><th>Texto corregido</th>'
        '<th>Momento</th></tr></thead><tbody>'
        + "".join(
            f"<tr><td class=\"mono\">{_e(item['sentence_id'])}</td>"
            f"<td>{_e(_DECISIONS.get(item['decision'], item['decision']))}</td>"
            f"<td>{_e(item.get('corrected_text') or '—')}</td><td>{_e(_friendly_time(item['timestamp']))}</td></tr>"
            for item in decisions
        )
        + "</tbody></table></div>"
    ) if decisions else '<p class="note">No se tomaron decisiones de revisión humana en este trabajo.</p>'
    cache = report.get("audio_cache") or {}
    cache_block = (_table((_CACHE_LABELS.get(name, name), value) for name, value in cache.items())
                   if cache else '<p class="note">Sin datos de reutilización de audio.</p>')
    validation_block = _table(
        (_VALIDATION_LABELS.get(name, name), _VALIDATION_VALUES.get(str(value), value))
        for name, value in validation.items()
    )
    metrics = report["metrics"]
    metric_block = _table(
        (_METRIC_LABELS.get(name, name), "no medido" if value is None else value)
        for name, value in metrics.items() if name != "note"
    )
    job_block = _table((
        ("Libro", title), ("Autor", book["author"] or "—"), ("Editorial", book["publisher"] or "—"),
        ("Edición", book["edition"] or "—"), ("Año", book["publication_year"] or "—"),
        ("Archivo de origen", report["source_name"]), ("Identificador del trabajo", report["job_id"]),
        ("Servicio de voz (TTS)", narration["tts_provider"] or "—"),
        ("Servicio de revisión (ASR)", narration["asr_provider"] or "—"),
        ("Intentos técnicos", totals["technical_attempts"]), ("Retomas de calidad", totals["quality_retakes"]),
        ("Frases con audio reutilizado", totals["reused_audio"]),
    ))
    log_block = f"<pre>{_e(log_excerpt)}</pre>" if log_excerpt else '<p class="note">Sin registro disponible.</p>'
    byline = " · ".join(part for part in (book["author"], book["publisher"], book["publication_year"]) if part)

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Informe de calidad · {_e(title)}</title>
<style>{_STYLE}</style></head>
<body><main class="wrap">
<p class="eyebrow">Informe de calidad del audiolibro</p>
<h1>{_e(title)}</h1>
<p class="by">{_e(byline or report['source_name'])}</p>

<section class="verdict {css}" role="status"><div class="icon" aria-hidden="true">{icon}</div>
<div><h2>{_e(headline)}</h2><p>{_e(explanation)}</p></div></section>
<div class="bar" aria-hidden="true">{bar}</div>
<ul class="legend">{legend}</ul>
<dl class="facts">{fact_list}</dl>

<h2>Necesitan tu atención</h2>
<p class="hint">Escucha cada frase y compárala con el texto. «147» y «ciento cuarenta y siete» cuentan como
coincidencia: aquí sólo aparecen diferencias reales o dudosas.</p>
<div class="attn">{attention}</div>

<h2>Todas las frases por capítulo</h2>
<div class="tools" role="group" aria-label="Filtrar frases">
  <button type="button" data-filter="all" aria-pressed="true">Todas</button>
  <button type="button" data-filter="correct" aria-pressed="false">Correctas</button>
  <button type="button" data-filter="corrected" aria-pressed="false">Corregidas</button>
  <button type="button" data-filter="auto" aria-pressed="false">Aceptadas automáticamente</button>
  <button type="button" data-filter="review" aria-pressed="false">Requieren revisión</button>
  <button type="button" data-filter="unresolved" aria-pressed="false">Sin resolver</button>
  <button type="button" data-filter="failed" aria-pressed="false">Fallos</button>
  <input type="search" id="q" placeholder="Buscar una palabra del texto" aria-label="Buscar frases">
  <span class="shown" id="shown" aria-live="polite"></span>
</div>
{"".join(blocks) or '<div class="card calm">Este trabajo no contiene frases.</div>'}

<details class="tech card"><summary>Detalles técnicos</summary><div class="tech-body">
<h3>Trabajo</h3>{job_block}
<h3>Decisiones de revisión humana</h3>{decision_block}
<h3>Reutilización de audio</h3>
<p class="note">El audio se guarda por frase y voz, así que corregir una frase no vuelve a pagar las demás.</p>
{cache_block}
<h3>Validación del paquete DAISY</h3>{validation_block}
<h3>Métricas propuestas</h3><p class="note">{_e(metrics['note'])}</p>{metric_block}
<h3>Análisis acústico</h3><p class="note">{_e(report['acoustic_analysis'])}</p>
<h3>Registro de ejecución</h3>{log_block}
</div></details>
</main>
<script>{_SCRIPT}</script></body></html>
"""


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)
