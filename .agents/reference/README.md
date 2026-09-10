# Reference material

Inputs the user supplied to guide the implementation. **Nothing here is project code**, is
imported by the package, or is covered by the test suite.

## `sample_vision_script.py`

The user's own working vision script, supplied on 2026-09-10 as the reference for how they
wanted the vision route to behave: an OpenAI-compatible endpoint (LM Studio at
`localhost:1234`), a base64 data URL per image, per-page PDF rasterisation, and a Markdown
result.

The implemented route in `src/ca_agent/vision/` keeps that shape but differs in three places,
all deliberate:

| This script | The implementation | Why |
|---|---|---|
| `fitz` (PyMuPDF) for rasterisation | `pypdfium2` | PyMuPDF is AGPL-3.0, incompatible with proprietary code (ADR-010) |
| `openai` SDK | `httpx` with an injected client | Lets retry and backoff be asserted from a recorded sleep sequence with zero network (ADR-011) |
| Free-form Markdown reply | Strict JSON schema, validated, then rendered to Markdown | Makes "did this extraction work" checkable rather than a judgement about prose; an endpoint that ignores `response_format` is still caught |

The prompt is also stricter than this script's: explicit rules against inferring an unreadable
value and against normalising identifiers, because a plausible invented amount in an audit file
is the worst output this pipeline could produce.

Keep this file for provenance. If the vision route is ever revisited, the differences above are
the decisions to revisit with it.
