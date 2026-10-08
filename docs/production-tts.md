# Production TTS: MeloTTS Spanish

## Selection

Phase 3 uses [MeloTTS](https://github.com/myshell-ai/MeloTTS) with its Spanish
speaker. The official project documents Spanish support, local/offline
inference, CPU real-time inference, and an MIT license for commercial and
non-commercial use. Coqui XTTS-v2 was not selected because its CPML model
license is non-commercial. Piper was not selected because Spanish voice
licenses vary by individual voice and dataset.

The model and its dependencies remain optional. MockTTS is still the default
and remains the implementation used by the normal test suite.

## Installation

On Windows with Python 3.12, the upstream MeloTTS metadata pins two packages
that do not provide compatible wheels (`fugashi==1.3.0` and
`mecab-python3==1.0.9`). Install the project first, then install MeloTTS and
the compatible Windows dependencies explicitly:

```powershell
python -m pip install -e ".[dev]"
python -m pip install fugashi==1.3.2 mecab-python3==1.0.12 tokenizers==0.13.3
python -m pip install --no-deps "MeloTTS @ git+https://github.com/myshell-ai/MeloTTS.git"
python -m pip install txtsplit torch torchaudio cached_path transformers==4.27.4 `
  num2words==0.5.12 unidic_lite==1.0.8 pykakasi==2.2.1 g2p_en==2.1.0 `
  anyascii==0.3.2 jamo==0.4.1 "gruut[de,es,fr]==2.2.3" g2pkk `
  librosa==0.9.1 pydub==0.25.1 eng_to_ipa==0.0.2 inflect==7.0.0 `
  unidecode==1.3.7 pypinyin==0.50.0 cn2an==0.5.22 jieba gradio `
  langid==1.1.6 tqdm tensorboard==2.16.2 loguru==0.7.2
```

The `--no-deps` step is intentional: it prevents the upstream package from
replacing the compatible Windows wheels with its source-only pins. Build
`tokenizers==0.13.3` with MSVC and Rust available on `PATH`; the tested build
used MSVC 19.44 and Rust 1.99 with `RUSTFLAGS=-A invalid_reference_casting`.
MeloTTS may download its Spanish checkpoint on first initialization.
Production deployments should pre-cache and review the exact checkpoint and
its metadata before using it with protected or copyrighted books.

## Lifecycle and inference

`MeloTTSEngine` loads the model lazily on the first synthesis request and
retains it for the lifetime of the worker process. Each sentence is synthesized
to a deterministic `<sentence-id>.wav` path. The DAISY pipeline then measures
the WAV with `AudioProcessor`; the TTS adapter does not duplicate audio
analysis.

Supported configuration includes device, language, voice, optional checkpoint
path, retry count, retry delay, cache reuse, and forced regeneration.

## Failure handling and resume

Valid existing WAV files are reused unless force regeneration is enabled.
Failures are logged with the sentence ID and retried. After retries are
exhausted, `TTSSynthesisError` is raised and the conversion fails explicitly.
Successful sentence files remain available for a later resumed run.

## Performance

Each successful request records synthesis time, generated audio duration, and
real-time factor (RTF). CPU and GPU performance depends on the installed
PyTorch build, hardware, and model checkpoint; benchmark the selected
deployment environment before committing to throughput targets.

## Production considerations

Review model/checkpoint terms, speaker/data provenance, book rights, and voice
permissions before deployment. The adapter does not implement voice cloning.
`model_path` may point to a local checkpoint file or a directory containing
the language configuration expected by MeloTTS. GPU acceleration requires a
CUDA-compatible PyTorch installation.
