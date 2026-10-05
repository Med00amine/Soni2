"""Structural DAISY package validation."""

from pathlib import Path
from urllib.parse import urldefrag

from lxml import etree

from app.ingestion.models import DaisyValidationReport
from app.audio.processor import AudioProcessor

SMIL_NS = {"s": "http://www.w3.org/2001/SMIL20/Language"}


class DaisyValidator:
    """Validate package files and cross-resource references."""

    def validate(self, package_dir: Path) -> DaisyValidationReport:
        package_dir = Path(package_dir)
        errors: list[str] = []
        warnings: list[str] = []
        required = ["content/book.xml", "book.ncx", "book.opf", "book.res"]
        errors.extend(f"Missing required file: {path}" for path in required if not (package_dir / path).is_file())
        if errors:
            return DaisyValidationReport(valid=False, errors=errors)
        try:
            content = etree.parse(str(package_dir / "content/book.xml"))
            ncx = etree.parse(str(package_dir / "book.ncx"))
            opf = etree.parse(str(package_dir / "book.opf"))
        except (OSError, etree.XMLSyntaxError) as exc:
            return DaisyValidationReport(valid=False, errors=[f"Invalid package XML: {exc}"])

        ids = [element.get("id") for element in content.getroot().iter() if element.get("id")]
        if len(ids) != len(set(ids)):
            errors.append("DTBook contains duplicate IDs.")
        text_ids = set(ids)
        sentence_ids = {
            element.get("id")
            for element in content.getroot().iter()
            if etree.QName(element).localname == "sent" and element.get("id")
        }
        referenced_text_ids: set[str] = set()
        referenced_audio_names: set[str] = set()
        smil_files = sorted(package_dir.glob("smil/*.smil"))
        audio_processor = AudioProcessor()
        for smil_path in smil_files:
            try:
                smil = etree.parse(str(smil_path))
            except etree.XMLSyntaxError as exc:
                errors.append(f"Invalid SMIL '{smil_path.name}': {exc}")
                continue
            for text in smil.xpath("//s:text", namespaces=SMIL_NS):
                _, fragment = urldefrag(text.get("src", ""))
                referenced_text_ids.add(fragment)
                if fragment not in text_ids:
                    errors.append(f"Broken text reference: {text.get('src')}")
            for audio in smil.xpath("//s:audio", namespaces=SMIL_NS):
                source = package_dir / "audio" / Path(audio.get("src", "")).name
                referenced_audio_names.add(source.name)
                if not source.is_file():
                    errors.append(f"Broken audio reference: {audio.get('src')}")
                    continue
                try:
                    duration = audio_processor.inspect(source).duration_seconds
                    begin = float(audio.get("clipBegin", "nan"))
                    end = float(audio.get("clipEnd", "nan"))
                    if begin < 0 or end <= begin or end > duration + 0.001:
                        errors.append(f"Invalid audio clip in '{smil_path.name}': {begin} to {end}")
                except (ValueError, OSError) as exc:
                    errors.append(str(exc))
        manifest = opf.xpath("//*[local-name()='manifest']/*[local-name()='item']")
        for item in manifest:
            href = item.get("href", "")
            if not (package_dir / href).is_file():
                errors.append(f"OPF references missing resource: {href}")
        missing_sentences = sentence_ids - referenced_text_ids
        errors.extend(f"Missing synchronization entry for text ID: {identifier}" for identifier in sorted(missing_sentences))
        for audio_path in (package_dir / "audio").glob("*"):
            if audio_path.is_file() and audio_path.name not in referenced_audio_names:
                warnings.append(f"Orphan audio file: {audio_path.name}")
        for nav in ncx.xpath("//*[local-name()='content']"):
            target = nav.get("src", "")
            resource = package_dir / target.split("#", 1)[0]
            if not resource.is_file():
                errors.append(f"NCX references missing resource: {target}")
            elif "#" in target:
                try:
                    target_tree = etree.parse(str(resource))
                    target_id = target.split("#", 1)[1]
                    if not target_tree.xpath(f"//*[@id={target_id!r}]"):
                        errors.append(f"NCX references missing target: {target}")
                except etree.XMLSyntaxError:
                    pass
        return DaisyValidationReport(valid=not errors, errors=errors, warnings=warnings)
