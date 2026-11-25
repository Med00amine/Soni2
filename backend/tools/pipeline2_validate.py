"""Run DAISY Pipeline 2's own checks on a DAISY 3 package, through its web service.

    python -m tools.pipeline2_validate <ZIP or package folder> --out <folder>

The DAISY Pipeline 2 engine must be running on this machine in local mode
(``pipeline2`` started with ``local``, or the engine's web service with
``PIPELINE2_WS_LOCALFS=true``), listening on http://localhost:8181/ws.

Pipeline 2 has no "DAISY 3 validator" script (ZedVal, the Z39.86 conformance
validator, is in Pipeline 1). What it does have, and what is run here:

* ``nimas-fileset-validator``: the package file (OPF) and every DTBook of
  the fileset, against the RelaxNG and Schematron schemas Pipeline 2 ships.
  NIMAS is a DAISY 3 text profile, so its NIMAS-only metadata rules (for
  example dc:Rights and dc:Source) are reported but are not DAISY 3 errors.
* ``dtbook-validator``: the DTBook 2005-3 document on its own.
* ``daisy3-to-epub3``: Pipeline 2 reading the whole DAISY 3 book (OPF, NCX,
  SMIL, DTBook and audio) and converting it; it fails on a book it cannot read.

Each job's status, messages and result files are saved in ``--out``; a
summary is printed and written to ``summary.json``. Nothing is sent anywhere
but the local engine.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

DATA = "{http://www.daisy.org/ns/pipeline/data}"


def _request(method: str, url: str, body: bytes | None = None, content_type: str = "application/xml") -> bytes:
    request = urllib.request.Request(url, data=body, method=method,
                                     headers={"Content-Type": content_type} if body is not None else {})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def _file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def _script_ports(base: str, script: str) -> tuple[set[str], set[str]]:
    """The script's input ports and options, as the engine describes them."""
    root = ET.fromstring(_request("GET", f"{base}/scripts/{script}"))
    return ({port.get("name") for port in root.iter(f"{DATA}input")},
            {option.get("name") for option in root.iter(f"{DATA}option")})


def _submit(base: str, script: str, options: dict[str, str]) -> str:
    """A job: a value whose name is an input port of the script is sent as that input."""
    inputs, _known = _script_ports(base, script)
    request = ET.Element(f"{DATA}jobRequest")
    ET.SubElement(request, f"{DATA}script", {"href": f"{base}/scripts/{script}"})
    for name, value in options.items():
        if name in inputs:
            port = ET.SubElement(request, f"{DATA}input", {"name": name})
            ET.SubElement(port, f"{DATA}item", {"value": value})
        else:
            option = ET.SubElement(request, f"{DATA}option", {"name": name})
            option.text = value
    body = ET.tostring(request, encoding="utf-8", xml_declaration=True)
    job = ET.fromstring(_request("POST", f"{base}/jobs", body))
    return job.get("id")


def _wait(base: str, job_id: str, timeout: float) -> ET.Element:
    deadline = time.monotonic() + timeout
    while True:
        job = ET.fromstring(_request("GET", f"{base}/jobs/{job_id}?msgSeq=0"))
        if job.get("status") not in {"IDLE", "RUNNING"} or time.monotonic() > deadline:
            return job
        time.sleep(3)


def _messages(job: ET.Element) -> list[dict]:
    return [{"level": message.get("level"), "text": " ".join((message.text or "").split())}
            for message in job.iter(f"{DATA}message")]


def run(package: Path, out: Path, base: str, timeout: float) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    opf = next(package.glob("*.opf"))
    dtbook = next(path for path in package.glob("*.xml"))
    jobs = {
        "nimas-fileset-validator": {"source": _file_uri(opf)},
        "dtbook-validator": {"source": _file_uri(dtbook)},
        # The DAISY 3 book read and its issues reported, not only converted.
        "daisy3-to-epub3": {"source": _file_uri(opf), "validation": "report"},
    }
    summary: dict = {"package": str(package), "engine": base, "jobs": {}}
    for script, options in jobs.items():
        inputs, known = _script_ports(base, script)
        options = {name: value for name, value in options.items() if name in inputs | known}
        job_id = _submit(base, script, options)
        job = _wait(base, job_id, timeout)
        status = job.get("status")
        folder = out / script
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "job.xml").write_bytes(ET.tostring(job, encoding="utf-8"))
        try:
            (folder / "job.log").write_bytes(_request("GET", f"{base}/jobs/{job_id}/log"))
        except urllib.error.URLError:
            pass
        try:
            archive = _request("GET", f"{base}/jobs/{job_id}/result")
            with zipfile.ZipFile(io.BytesIO(archive)) as results:
                results.extractall(folder / "results")
        except (urllib.error.URLError, zipfile.BadZipFile):
            pass
        statuses = []
        for path in (folder / "results").rglob("*") if (folder / "results").exists() else []:
            if path.is_file() and path.suffix == ".xml" and "status" in path.parent.name + path.name:
                try:
                    statuses.append(ET.parse(path).getroot().get("result"))
                except ET.ParseError:
                    pass
        messages = _messages(job)
        summary["jobs"][script] = {
            "job_id": job_id, "status": status, "validation_status": statuses,
            "errors": [item for item in messages if item["level"] in {"ERROR", "WARNING"}][:60],
            "options": options,
        }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("package", type=Path, help="the delivery ZIP, or a folder with the DAISY 3 package")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--engine", default="http://localhost:8181/ws")
    parser.add_argument("--timeout", type=float, default=1800)
    arguments = parser.parse_args()
    source = arguments.package
    with tempfile.TemporaryDirectory(prefix="soni2-p2-") as folder:
        if source.suffix.lower() == ".zip":
            with zipfile.ZipFile(source) as archive:
                archive.extractall(folder)
            source = next(path.parent for path in Path(folder).rglob("*.opf"))
        summary = run(source, arguments.out, arguments.engine.rstrip("/"), arguments.timeout)
    for script, result in summary["jobs"].items():
        print(f"{script}: {result['status']} {result['validation_status'] or ''}")
        for item in result["errors"][:10]:
            print(f"  {item['level']}: {item['text'][:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
