The previous model response did not comply with the required JSON schema.

Regenerate the answer from the original review task.

Rules:
- Return only a JSON array
- Do not add commentary
- Do not use markdown fences
- Do not invent findings that are not supported by the reviewed code
- Every item must contain exactly these fields:
  - `description`
  - `file`
  - `line`
  - `severity`
  - `category_detail`
  - `confidence`
- `file` must be one of the allowed files below
- `line` must be a positive integer
- `severity` must be one of: `low`, `medium`, `high`, `critical`
- `confidence` must be a number from `0.0` to `1.0`

Allowed files:

{{allowed_files}}

Previous invalid output:

{{bad_output}}

Original review prompt:

{{review_prompt}}
