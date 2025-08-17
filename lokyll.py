#!/usr/bin/env python3
import argparse
import os
import re
import shutil
import sys
import time
import subprocess
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Comment
import yaml

# --- Translation backend: Argos Translate (offline, free, OSS)
try:
    from argostranslate import translate as argos_translate
except ImportError as e:
    raise SystemExit("Please install argostranslate: pip install argostranslate") from e


# --------------- Utilities

HTML_EXTS = {".html", ".htm"}
MD_EXTS = {".md", ".markdown", ".mdx"}  # mdx won’t be parsed for JSX, just text heuristics
COPY_ALWAYS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".woff", ".woff2", ".ttf", ".eot"
}

# For this repo (Jekyll), content lives largely in Markdown/Liquid
LIQUID_TAG_PATTERN = re.compile(r"({%-?.*?-?%}|{{.*?}})", re.DOTALL)
LIQUID_TAG_W_SPACE_PATTERN = re.compile(r"(\s*)({%-?.*?-?%}|{{.*?}})(\s*)", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`]+`")           # inline code in Markdown
FENCED_BLOCK_RE = re.compile(r"(^|\n)```.*?\n.*?\n```", re.DOTALL)  # fenced code blocks
HTML_ENTITY_RE = re.compile(r"&[a-zA-Z]+;|&#\d+;")
URL_LIKE_RE = re.compile(r"https?://|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")

# JS heuristics: translate string literals assigned to innerHTML/textContent or used in document.write/insertAdjacentHTML
JS_TRANSLATABLE_RE = re.compile(
    r"""(?P<prefix>\b(?:innerHTML|outerHTML|textContent)\s*=\s*|document\.write\s*\(|insertAdjacentHTML\s*\(\s*['"][^'"]+['"]\s*,\s*|=\s*)"""
    r"""(?P<quote>['"`])(?P<text>(?:\\.|(?!\1).)*?)(?P=quote)""",
    re.DOTALL
)

# Attributes in HTML that often carry user-facing text
TRANSLATABLE_ATTRS = [
    "title", "alt", "placeholder", "aria-label", "aria-placeholder",
    "aria-description", "aria-valuetext", "content"  # for <meta name="description">, etc.
]


def build_translator(from_lang: str, to_lang: str):
    models = argos_translate.get_installed_languages()
    src = next((l for l in models if l.code == from_lang), None)
    dst = next((l for l in models if l.code == to_lang), None)
    if not src or not dst:
        raise SystemExit(
            f"Argos language packs not installed for {from_lang}→{to_lang}. "
            "Install with Argos (see usage section)."
        )
    return src.get_translation(dst)


def should_copy_as_is(path: Path) -> bool:
    if path.suffix.lower() in COPY_ALWAYS:
        return True
    # ignore everything that isn't html/md/js/css/liquid layout includes; we only write translated files or copy assets
    return False


def safe_segments(text: str):
    """
    Split text into non-liquid parts and liquid tags, preserving liquid blocks untouched.
    Returns list of (is_liquid, segment).
    """
    parts = []
    last = 0
    for m in LIQUID_TAG_PATTERN.finditer(text):
        if m.start() > last:
            parts.append((False, text[last:m.start()]))
        parts.append((True, m.group(0)))
        last = m.end()
    if last < len(text):
        parts.append((False, text[last:]))
    return parts


def translate_string(tfunc, s: str) -> str:
    """Translate a string while skipping URLs, HTML entities and keeping spacing."""
    if not s or s.isspace():
        return s
    # Quick guards: if looks like code/URL-heavy, skip
    if URL_LIKE_RE.search(s):
        return s
    # Split by HTML entities to keep them intact
    chunks = HTML_ENTITY_RE.split(s)
    entities = HTML_ENTITY_RE.findall(s)
    special_chars = ['#', '$', '@', '!', '%', '^', '&', '*']
    out = []
    for i, chunk in enumerate(chunks):
        chunk_strip = chunk.strip()
        if chunk_strip and chunk_strip not in special_chars:
            try:
                translated = tfunc(chunk_strip)
            except Exception:
                translated = chunk  # be safe
            # Re-apply leading/trailing whitespace of chunk
            lpad = len(chunk) - len(chunk.lstrip())
            rpad = len(chunk) - len(chunk.rstrip())
            translated = (" " * lpad) + translated + (" " * rpad)
            out.append(translated)
        else:
            out.append(chunk)
        if i < len(entities):
            out.append(entities[i])
    return "".join(out)


# --------------- HTML translation

def translate_html(tfunc, html: str) -> str:
    # 1. Separate front matter (if exists) from HTML content
    front_matter_end = 0
    if html.startswith("---\n"):
        end = html.find("\n---", 4)
        if end != -1:
            front_matter_end = end + 4  # Keep the closing "---"

    front_matter = html[:front_matter_end] if front_matter_end > 0 else ""
    body_html = html[front_matter_end:]

    # 2. Process Liquid tags in the BODY only
    liquid_blocks = []
    def liquid_repl(m):
        leading_ws = m.group(1)
        tag = m.group(2)
        trailing_ws = m.group(3)
        liquid_blocks.append((leading_ws, tag, trailing_ws))
        return f"LIQUID_BLOCK_{len(liquid_blocks)-1}__"

    result_html = LIQUID_TAG_W_SPACE_PATTERN.sub(liquid_repl, body_html)

    # 3. Parse and translate the BODY only
    soup = BeautifulSoup(result_html, 'html.parser')

    def is_translatable_text(node):
        if not isinstance(node, NavigableString):
            return False
        parent = node.parent
        if parent and parent.name in ("script", "style", "code", "pre"):
            return False
        return str(node).strip() != ""  # Now we allow LIQUID_BLOCK placeholders

    # 4. Translate text nodes, skipping LIQUID_BLOCK parts
    for txt in soup.find_all(string=is_translatable_text):
        original_text = str(txt)
        if "LIQUID_BLOCK_" not in original_text:
            # Simple case: No placeholders → translate directly
            translated_text = translate_string(tfunc, original_text)
            if original_text != translated_text:
                result_html = result_html.replace(original_text, translated_text, 1)
        else:
            # Complex case: Split around LIQUID_BLOCK, translate the rest
            parts = re.split(r'(LIQUID_BLOCK_\d+__)', original_text)
            translated_parts = []
            for part in parts:
                if re.fullmatch(r'LIQUID_BLOCK_\d+__', part):
                    translated_parts.append(part)  # Keep placeholders as-is
                else:
                    translated_parts.append(translate_string(tfunc, part))  # Translate text
            translated_text = ''.join(translated_parts)
            if original_text != translated_text:
                result_html = result_html.replace(original_text, translated_text, 1)

    # 5. Translate attributes (similar logic)
    for tag in soup.find_all(True):
        for attr in TRANSLATABLE_ATTRS:
            if tag.has_attr(attr):
                val = tag[attr]
                if isinstance(val, list):
                    continue
                if "LIQUID_BLOCK_" in val:
                    # Split, translate non-LIQUID parts, recombine
                    parts = re.split(r'(LIQUID_BLOCK_\d+__)', val)
                    translated_val = ''.join(
                        part if re.fullmatch(r'LIQUID_BLOCK_\d+__', part)
                        else translate_string(tfunc, part)
                        for part in parts
                    )
                else:
                    translated_val = translate_string(tfunc, val)
                if translated_val != val:
                    result_html = result_html.replace(f'{attr}="{val}"', f'{attr}="{translated_val}"', 1)

    # 5. Restore Liquid tags in body
    for i, (leading_ws, tag, trailing_ws) in enumerate(liquid_blocks):
        placeholder = f"LIQUID_BLOCK_{i}__"
        result_html = result_html.replace(placeholder, f"{leading_ws}{tag}{trailing_ws}")

    # 6. Recombine (front matter untouched + translated body)
    return front_matter + result_html


# --------------- Markdown translation (front-matter aware)

def split_front_matter(md_text: str):
    """Read yaml lines at the beginning of file"""
    if md_text.startswith("---\n"):
        end = md_text.find("\n---", 4)
        if end != -1:
            fm = md_text[4:end]
            body = md_text[end+4:]
            return fm, body
    return None, md_text

def translate_markdown(tfunc, md_text: str) -> str:
    """Optimized Markdown translator with precise whitespace and code preservation"""
    # 1. Split front matter
    fm_raw, body = split_front_matter(md_text)

    # 2. Process front matter
    if fm_raw is not None:
        try:
            data = yaml.safe_load(fm_raw) or {}
        except Exception:
            data = {}
        for key in ("title", "description", "summary"):
            if key in data and isinstance(data[key], str) and not LIQUID_TAG_PATTERN.search(data[key]):
                data[key] = translate_string(tfunc, data[key])
        fm_out = "---\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip() + "\n---"
    else:
        fm_out = None

    # 3. Protection patterns (links excluded here, handled separately)
    PROTECT_PATTERN = re.compile(
        r'(?P<fence>^```[\s\S]*?^```)|'  # Fenced code blocks
        r'(?P<inline>`[^`]+`)|'          # Inline code (just the code part)
        r'(?P<liq>{%.*?%}|{{.*?}})',     # Liquid tags
        re.MULTILINE
    )

    protected_segments = []

    def protector(match):
        protected_segments.append(match.group(0))
        return f"[[[PROTECTED_{len(protected_segments)-1}]]]"

    body_protected = PROTECT_PATTERN.sub(protector, body)

    # 3b. Special handling for markdown links: translate label, keep URL intact
    LINK_PATTERN = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')

    def link_repl(match):
        label = match.group(1)
        url = match.group(2)
        translated_label = translate_string(tfunc, label)
        return f"[{translated_label}]({url})"

    body_protected = LINK_PATTERN.sub(link_repl, body_protected)

    # 4. Improved translation with precise whitespace handling
    translated_lines = []
    for line in body_protected.splitlines(True):
        if not line.strip():
            translated_lines.append(line)
            continue

        # Headings → preserve # but translate the text
        if line.lstrip().startswith('#'):
            leading_ws = line[:len(line)-len(line.lstrip())]
            hashes, _, text = line.lstrip().partition(" ")
            if text.strip():
                text_translated = translate_string(tfunc, text.strip())
            else:
                text_translated = text
            translated_lines.append(f"{leading_ws}{hashes} {text_translated}\n")
            continue

        # Normal text lines
        leading_ws = line[:len(line)-len(line.lstrip())]
        trailing_ws = line[len(line.rstrip()):] if line.rstrip() else line[-1]
        content = line.strip()
        if content:
            translated = translate_string(tfunc, content)
            translated = translated.replace('] (', '](')
            translated_lines.append(leading_ws + translated + trailing_ws)
        else:
            translated_lines.append(line)

    body_translated = ''.join(translated_lines)

    # 5. Restore protected content
    def restore_protected(match):
        idx = int(match.group(1))
        return protected_segments[idx]

    body_translated = re.sub(
        r'\[\[\[PROTECTED_(\d+)\]\]\]',
        restore_protected,
        body_translated
    )

    # 6. Final cleanup (ensure no double spaces in headings)
    body_translated = re.sub(r'^(#+)\s+#', r'\1 ', body_translated, flags=re.MULTILINE)

    # 7. Combine results
    if fm_out:
        return fm_out + ("\n" if not body_translated.startswith("\n") else "") + body_translated
    return body_translated


# --------------- JS translation (heuristic)

def translate_js_heuristic(tfunc, js_text: str) -> str:
    def repl(m):
        text = m.group("text")
        quote = m.group("quote")

        # Skip very short or URL-like strings
        raw = text
        if URL_LIKE_RE.search(raw) or len(raw.strip()) < 4:
            return m.group(0)

        # If it looks like HTML or long plain text, translate
        looks_html = "<" in raw and ">" in raw
        longish = len(raw.split()) >= 4
        if not (looks_html or longish):
            return m.group(0)

        # Keep ${} placeholders inside template literals
        if quote == "`":
            parts = re.split(r"(\$\{.*?\})", raw)
            translated_parts = []
            for p in parts:
                if p.startswith("${") and p.endswith("}"):
                    translated_parts.append(p)
                else:
                    translated_parts.append(translate_string(tfunc, p))
            new_text = "".join(translated_parts)
        else:
            new_text = translate_string(tfunc, raw)

        # Escape quotes/backticks appropriately
        escaped = new_text.replace("\\", "\\\\")
        if quote == "'":
            escaped = escaped.replace("'", "\\'")
        elif quote == '"':
            escaped = escaped.replace('"', '\\"')
        else:  # backtick
            escaped = escaped.replace("`", "\\`")

        return f"{m.group('prefix')}{quote}{escaped}{quote}"

    return JS_TRANSLATABLE_RE.sub(repl, js_text)


# --------------- Main processing

def process_tree(src_dir: Path, dest_dir: Path, from_lang: str, to_lang: str,
                 include_markdown: bool, translate_js: bool):
    t = build_translator(from_lang, to_lang).translate

    # First pass to count total files
    print("Scanning directory structure...")
    total_files = 0
    for root, _, files in os.walk(src_dir):
        total_files += len(files)

    processed_files = 0
    start_time = time.time()
    terminal_width = shutil.get_terminal_size().columns

    for root, dirs, files in os.walk(src_dir):
        rel_root = Path(root).relative_to(src_dir)
        out_root = dest_dir / rel_root
        out_root.mkdir(parents=True, exist_ok=True)

        for name in files:
            src_path = Path(root) / name
            rel_path = src_path.relative_to(src_dir)
            ext = src_path.suffix.lower()

            # Update progress
            processed_files += 1
            progress = processed_files / total_files
            bar_length = 40
            filled_length = int(bar_length * progress)
            bar = '█' * filled_length + '-' * (bar_length - filled_length)
            percent = int(100 * progress)
            elapsed = time.time() - start_time
            files_per_sec = processed_files / elapsed if elapsed > 0 else 0

            # Clear the line before printing
            print(f"\r{' ' * (terminal_width - 1)}\r", end="", flush=True)

            # Print new progress info
            display_name = name[:20] + ('...' if len(name) > 20 else '')
            print(
                f"\rTranslating... |{bar}| {percent}% "
                f"({processed_files}/{total_files}) "
                f"[{elapsed:.1f}s, {files_per_sec:.1f} files/s] "
                f"{display_name}",
                end="",
                flush=True
            )

            try:
                # Copy assets as-is
                if should_copy_as_is(src_path):
                    shutil.copy2(src_path, out_root / name)
                    continue

                # HTML
                if ext in HTML_EXTS:
                    text = src_path.read_text(encoding="utf-8", errors="ignore")
                    translated = translate_html(t, text)
                    (out_root / name).write_text(translated, encoding="utf-8")
                    continue

                # Markdown (optional; strongly recommended for this repo)
                if include_markdown and ext in MD_EXTS:
                    text = src_path.read_text(encoding="utf-8", errors="ignore")
                    translated = translate_markdown(t, text)
                    (out_root / name).write_text(translated, encoding="utf-8")
                    continue

                # JavaScript (optional heuristics)
                if translate_js and ext in {".js", ".mjs", ".ts"}:
                    text = src_path.read_text(encoding="utf-8", errors="ignore")
                    translated = translate_js_heuristic(t, text)
                    (out_root / name).write_text(translated, encoding="utf-8")
                    continue

                # Everything else: by default, ignore (keeps new repo lean)
                # If you prefer to copy everything else, uncomment the next line:
                shutil.copy2(src_path, out_root / name)

            except Exception as e:
                print(f"\nError processing {src_path}: {str(e)}\n", flush=True)
                time.sleep(1)

    # Clear the progress line and print completion message
    print(f"\r{' ' * (terminal_width - 1)}\r", end="", flush=True)
    print(f"✓ Translation complete! Processed {total_files} files in {time.time() - start_time:.1f} seconds")

def clone_if_needed(repo_url: str, dest: Path) -> Path:
    if dest.exists():
        return dest
    subprocess.run(["git", "clone", "--depth", "1", repo_url, str(dest)], check=True)
    return dest


def main():
    ap = argparse.ArgumentParser(description="Translate a website repo into another language (HTML + optional Markdown/JS).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--src", help="Path to a local source repo")
    src.add_argument("--repo-url", help="Git URL to clone from (shallow clone)")

    ap.add_argument("--dest", required=True, help="Destination folder (new translated repo)")
    ap.add_argument("--from-lang", required=True, help="Source language code (e.g. en)")
    ap.add_argument("--to-lang", required=True, help="Target language code (e.g. pt)")
    ap.add_argument("--include-markdown", action="store_true", help="Translate Markdown files (recommended for Jekyll sites)")
    ap.add_argument("--translate-js", action="store_true", help="Try translating JS string literals that render HTML/text")
    args = ap.parse_args()

    dest_dir = Path(args.dest)
    if dest_dir.exists() and any(dest_dir.iterdir()):
        raise SystemExit(f"Destination {dest_dir} already exists and is not empty.")

    if args.src:
        src_dir = Path(args.src).resolve()
        if not src_dir.exists():
            raise SystemExit(f"Source path not found: {src_dir}")
    else:
        # clone into a temp folder next to dest
        src_dir = Path(args.dest + "__src_clone").resolve()
        clone_if_needed(args.repo_url, src_dir)  # will create folder

    try:
        process_tree(src_dir, dest_dir, args.from_lang, args.to_lang, args.include_markdown, args.translate_js)
        # Create a minimal README in the new repo
        readme = f"""# Translated site
Source: {args.src or args.repo_url}
Language: {args.from_lang} → {args.to_lang}

Generated by [Lokyll](https://github.com/dfop02/Lokyll). Content files translated; assets copied.
"""
        (dest_dir / "README_TRANSLATED.md").write_text(readme, encoding="utf-8")
        print(f"Done. New repo at: {dest_dir}")
    except KeyboardInterrupt:
        print("\nScript manually aborted.")
    finally:
        # Clean up shallow clone if we created it
        if not args.src and src_dir.exists():
            shutil.rmtree(src_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
