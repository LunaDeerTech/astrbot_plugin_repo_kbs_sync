"""Small, dependency-free MDX-to-Markdown normalizer.

The knowledge-base uploader only needs the document's readable Markdown.  A
full JavaScript/JSX runtime would be both expensive and unsafe in a bot
plugin, so this module removes MDX module declarations, JSX component tags,
and JSX expressions while preserving Markdown, front matter, fenced code, and
inline code.
"""

from __future__ import annotations

import re


_FENCE_START = re.compile(r"^[ \t]*(?P<mark>`{3,}|~{3,})")
_INLINE_CODE = re.compile(
    r"(?P<ticks>`+)(?P<body>[^`\n]*?)(?P=ticks)",
)
_MODULE_START = re.compile(r"^[ \t]*(?:import|export)\b")
_JSX_TAG_START = re.compile(
    r"<(?P<closing>/)?(?P<name>[A-Za-z][A-Za-z0-9_.:-]*)",
)


def preprocess_mdx_text(source: str) -> str:
    """Convert the useful, human-readable portion of MDX into Markdown.

    This intentionally does not try to evaluate JavaScript.  Components are
    unwrapped (their children remain), self-closing components disappear, and
    expressions are omitted.  The operation is deterministic and safe to run
    on untrusted repository content.
    """

    if not isinstance(source, str):
        raise TypeError("MDX source must be a string")

    protected: list[str] = []

    def protect(value: str) -> str:
        token = f"\x00REPO_KBS_SYNC_{len(protected)}\x00"
        protected.append(value)
        return token

    text = _protect_front_matter(source, protect)
    text = _protect_fenced_code(text, protect)
    text = _INLINE_CODE.sub(lambda match: protect(match.group(0)), text)

    text = _strip_module_declarations(text)
    text = _strip_jsx_tags(text)
    text = _strip_jsx_expressions(text)

    # Keep normal Markdown paragraph spacing without allowing removed MDX
    # declarations to leave a large blank area in the indexed document.
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))

    for index, value in enumerate(protected):
        text = text.replace(f"\x00REPO_KBS_SYNC_{index}\x00", value)
    return text


def preprocess_mdx_bytes(source: bytes) -> bytes:
    """Decode UTF-8 MDX, normalize it, and encode it as UTF-8 Markdown."""

    try:
        decoded = source.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("MDX 文件不是有效的 UTF-8 编码，无法预处理。") from exc
    return preprocess_mdx_text(decoded).encode("utf-8")


def _protect_front_matter(source: str, protect) -> str:
    lines = source.splitlines(keepends=True)
    if not lines:
        return source
    first = lines[0].lstrip("\ufeff").strip()
    if first != "---":
        return source

    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            return protect("".join(lines[: index + 1])) + "".join(lines[index + 1 :])
    # Preserve malformed/unclosed front matter instead of interpreting its
    # values as JSX or JavaScript.
    return protect(source)


def _protect_fenced_code(source: str, protect) -> str:
    lines = source.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        match = _FENCE_START.match(lines[index])
        if not match:
            output.append(lines[index])
            index += 1
            continue

        marker = match.group("mark")
        marker_char = marker[0]
        marker_length = len(marker)
        end = index + 1
        while end < len(lines):
            closing = _FENCE_START.match(lines[end])
            if (
                closing
                and closing.group("mark")[0] == marker_char
                and len(closing.group("mark")) >= marker_length
            ):
                end += 1
                break
            end += 1

        block = "".join(lines[index:end])
        token = protect(block)
        if block.endswith(("\n", "\r")):
            token += "\n"
        output.append(token)
        index = end
    return "".join(output)


def _strip_module_declarations(source: str) -> str:
    lines = source.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not _MODULE_START.match(line):
            output.append(line)
            index += 1
            continue

        balance = _delimiter_balance(line)
        index += 1
        if balance <= 0:
            continue

        while index < len(lines):
            balance += _delimiter_balance(lines[index])
            index += 1
            if balance <= 0:
                break
    return "".join(output)


def _delimiter_balance(source: str) -> int:
    balance = 0
    quote: str | None = None
    escaped = False
    for char in source:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char in "{([":
            balance += 1
        elif char in "})]":
            balance -= 1
    return balance


def _strip_jsx_tags(source: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(source):
        if source[index] != "<":
            output.append(source[index])
            index += 1
            continue

        if source.startswith("<>", index) or source.startswith("</>", index):
            index += 2 if source.startswith("<>", index) else 3
            continue

        match = _JSX_TAG_START.match(source, index)
        if not match:
            output.append(source[index])
            index += 1
            continue

        name = match.group("name")
        # Lowercase tags are valid HTML embedded in Markdown.  MDX component
        # names conventionally start with an uppercase letter; dotted and
        # namespaced names are also component-like.
        is_component = name[0].isupper() or "." in name or ":" in name
        if not is_component:
            output.append(source[index])
            index += 1
            continue

        end = _find_tag_end(source, match.end())
        if end is None:
            output.append(source[index])
            index += 1
            continue
        index = end + 1
    return "".join(output)


def _find_tag_end(source: str, start: int) -> int | None:
    quote: str | None = None
    escaped = False
    expression_depth = 0
    for index in range(start, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "{":
            expression_depth += 1
        elif char == "}" and expression_depth:
            expression_depth -= 1
        elif char == ">" and expression_depth == 0:
            return index
    return None


def _strip_jsx_expressions(source: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(source):
        if source[index] != "{":
            output.append(source[index])
            index += 1
            continue

        end = _find_expression_end(source, index + 1)
        if end is None:
            output.append(source[index])
            index += 1
            continue

        expression = source[index + 1 : end]
        if _looks_like_mdx_expression(expression):
            index = end + 1
            continue

        output.append(source[index : end + 1])
        index = end + 1
    return "".join(output)


def _find_expression_end(source: str, start: int) -> int | None:
    depth = 1
    quote: str | None = None
    escaped = False
    index = start
    while index < len(source):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _looks_like_mdx_expression(expression: str) -> bool:
    stripped = expression.strip()
    if not stripped:
        return True
    if stripped.startswith(("/*", "//", "...")):
        return True
    if re.search(r"(?:=>|&&|\|\||===|!==|\?\s|;|</?[A-Za-z])", stripped):
        return True
    if re.search(r"[+\-*%/=!<>]", stripped):
        return True
    # A bare identifier or a normal JavaScript expression is much more likely
    # to be MDX than a Markdown literal such as {#1}.
    return bool(re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", stripped))
