import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import anthropic
import yaml
from dotenv import load_dotenv
from flask import Flask, render_template, request, send_file, send_from_directory, abort, jsonify
from github import Github, GithubException

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB

BASE_DIR = Path(__file__).parent
SYSTEM_PROMPT = (
    (BASE_DIR / "devnote-style-guide.md").read_text()
    + "\n\n"
    + (BASE_DIR / "ingest.md").read_text()
)

FIGURE_EXTS = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"}
DATA_EXTS = {".csv", ".xlsx", ".xls"}
NOTEBOOK_EXTS = {".ipynb"}
SEQUENCE_EXTS = {".gb", ".dna"}
NARRATIVE_EXTS = {".md", ".docx", ".txt"}

BLOCKING_KEYWORDS = {"composition", "figure", "specification", "author", "notebook error"}

REPO_BY_LICENSE = {
    "CC-BY-4.0": "antonrmolina/devnotes-ccby-2026",
    "CERN-OHL-P-2.0": "antonrmolina/devnotes-cern-2026",
}

SKIP_NAMES = {"content.md", "upload.zip", "review-flags.json", "stable_id"}
SKIP_EXTS = {".docx", ".zip", ".env"}


def build_inventory(root: Path) -> dict:
    inventory = {"narrative": [], "notebooks": [], "data": [], "sequences": [], "figures": [], "other": []}
    for p in sorted(root.rglob("*")):
        if p.is_dir() or p.name.startswith("."):
            continue
        rel = str(p.relative_to(root))
        ext = p.suffix.lower()
        if ext in NARRATIVE_EXTS:
            inventory["narrative"].append(rel)
        elif ext in NOTEBOOK_EXTS:
            inventory["notebooks"].append(rel)
        elif ext in DATA_EXTS:
            inventory["data"].append(rel)
        elif ext in SEQUENCE_EXTS:
            inventory["sequences"].append(rel)
        elif ext in FIGURE_EXTS:
            inventory["figures"].append(rel)
        else:
            inventory["other"].append(rel)
    return inventory


def format_inventory(inv: dict) -> str:
    lines = []
    for category, files in inv.items():
        if files:
            lines.append(f"{category}:")
            for f in files:
                lines.append(f"  - {f}")
    return "\n".join(lines) if lines else "(no files found)"


def parse_review_flags(text: str) -> list:
    flags = []
    lines = text.splitlines()
    current_heading = "general"
    for line in lines:
        heading_match = re.match(r"^#{1,3}\s+(.+)", line)
        if heading_match:
            current_heading = heading_match.group(1).strip()
        review_match = re.search(r"REVIEW:\s*(.+)", line)
        if review_match:
            reason = review_match.group(1).strip().rstrip("\"'")
            field_match = re.match(r"^(\w[\w\s\-]*):\s*REVIEW:", line.strip())
            field = field_match.group(1).strip() if field_match else current_heading
            severity = "blocking" if any(k in field.lower() or k in reason.lower() for k in BLOCKING_KEYWORDS) else "advisory"
            flags.append({"field": field, "reason": reason, "severity": severity})
    return flags


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}


@app.route("/")
def upload():
    return render_template("upload.html")


@app.route("/submit", methods=["POST"])
def submit():
    zip_file = request.files.get("zip_file")
    author_name = request.form.get("author_name", "").strip()
    author_email = request.form.get("author_email", "").strip()
    affiliation = request.form.get("affiliation", "").strip()
    license_val = request.form.get("license", "CC-BY-4.0").strip()

    if not zip_file or zip_file.filename == "":
        return "No zip file provided", 400
    if not zip_file.filename.lower().endswith(".zip"):
        return "File must be a .zip archive", 400

    session_id = str(uuid.uuid4())
    session_dir = Path(f"/tmp/{session_id}")
    session_dir.mkdir(parents=True, exist_ok=True)

    zip_path = session_dir / "upload.zip"
    zip_file.save(str(zip_path))

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(session_dir)
    zip_path.unlink()

    # Run pandoc on any .docx files
    for docx_path in list(session_dir.rglob("*.docx")):
        try:
            result = subprocess.run(
                ["pandoc", str(docx_path), "-o", "content.md", "--extract-media=figures/"],
                cwd=str(docx_path.parent),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                app.logger.warning(f"pandoc warning for {docx_path.name}: {result.stderr}")
        except FileNotFoundError:
            app.logger.warning("pandoc not found — skipping .docx conversion")
        except subprocess.TimeoutExpired:
            app.logger.warning(f"pandoc timed out on {docx_path.name}")

    # Find primary content file
    content = ""
    generated_md = list(session_dir.rglob("content.md"))
    if generated_md:
        content_path = generated_md[0]
    else:
        md_files = [p for p in session_dir.rglob("*.md") if not p.name.startswith(".")]
        content_path = md_files[0] if md_files else None

    if content_path:
        content = content_path.read_text(errors="replace")
    else:
        notebooks = list(session_dir.rglob("*.ipynb"))
        if notebooks:
            content = f"(No narrative document found. Primary source appears to be notebook: {notebooks[0].name})"

    inventory = build_inventory(session_dir)
    inventory_str = format_inventory(inventory)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    user_message = f"""Convert the following research materials into a Nucleus DevNote following the skill and style guide above.

File inventory:
{inventory_str}

Author information provided by submitter:
- Name: {author_name}
- Email: {author_email}
- Affiliation: {affiliation}
- License: {license_val}

Use the provided author information for the frontmatter. Do not flag author fields as REVIEW unless email is missing.

Primary content:
{content}

Return only the complete index.md content starting with ---. Nothing before the opening --- and nothing after the closing content."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    index_md = message.content[0].text.strip()

    # Strip markdown code fence wrapper if Claude added one despite instructions
    if index_md.startswith("```"):
        lines = index_md.split("\n")
        if lines[-1].strip() == "```":
            index_md = "\n".join(lines[1:-1]).strip()

    # Ensure title value is quoted to prevent YAML parse errors on colons
    index_md = re.sub(
        r'^(title:\s*)(.+)$',
        lambda m: m.group(1) + '"' + m.group(2).strip().strip('"') + '"',
        index_md,
        flags=re.MULTILINE,
    )

    # Fix figure paths: if Claude wrote figures/X but only X exists at root, strip prefix
    figure_files = {p.name: p.relative_to(session_dir) for p in session_dir.rglob("*")
                    if p.is_file() and p.suffix.lower() in FIGURE_EXTS}
    def fix_figure_path(m):
        prefix, path = m.group(1), m.group(2)
        name = Path(path).name
        if name in figure_files and not (session_dir / path).exists():
            return prefix + str(figure_files[name])
        return m.group(0)
    index_md = re.sub(r'(thumbnail:\s*)(\S+)', fix_figure_path, index_md)
    index_md = re.sub(r'(:{3,}\{figure\}\s*)(\S+)', fix_figure_path, index_md)

    # Generate deterministic id from author email + title (stable across resubmissions)
    fm_for_id = parse_frontmatter(index_md)
    raw_title = str(fm_for_id.get("title", "untitled")).strip('"')
    raw_date = str(fm_for_id.get("date", "2026"))
    raw_license = str(fm_for_id.get("license", "CC-BY-4.0"))
    year = raw_date[:4] if len(raw_date) >= 4 else "2026"
    title_slug = re.sub(r"[^a-z0-9]+", "-", raw_title.lower()).strip("-")
    hash_suffix = hashlib.sha256(
        f"{author_email}:{title_slug}".encode()
    ).hexdigest()[:8]
    stable_id = f"dn-{year}-{hash_suffix}"

    index_md = re.sub(r"^id:.*$", f"id: {stable_id}", index_md, flags=re.MULTILINE)
    if not re.search(r"^id:", index_md, re.MULTILINE):
        index_md = index_md.rstrip() + f"\nid: {stable_id}\n"

    (session_dir / "index.md").write_text(index_md)
    (session_dir / "stable_id").write_text(stable_id)

    review_flags = parse_review_flags(index_md)
    (session_dir / "review-flags.json").write_text(json.dumps(review_flags))

    return render_template(
        "preview.html",
        index_md=index_md,
        review_flags=review_flags,
        session_id=session_id,
        file_inventory=inventory,
    )


@app.route("/publish/<session_id>", methods=["POST"])
def publish(session_id):
    if not re.match(r"^[0-9a-f\-]{36}$", session_id):
        abort(400)
    session_dir = Path(f"/tmp/{session_id}")
    if not session_dir.exists():
        return jsonify({"error": "Session not found"}), 404

    try:
        index_md_path = session_dir / "index.md"
        if not index_md_path.exists():
            return jsonify({"error": "index.md not found in session"}), 404
        index_md = index_md_path.read_text()

        fm = parse_frontmatter(index_md)
        title = str(fm.get("title", "Untitled DevNote")).strip('"')
        license_val = str(fm.get("license", "CC-BY-4.0"))
        stable_id_path = session_dir / "stable_id"
        if stable_id_path.exists():
            devnote_id = stable_id_path.read_text().strip()
        else:
            raw_id = fm.get("id", session_id[:8])
            devnote_id = re.sub(r"[^a-z0-9\-]", "-", str(raw_id).lower()).strip("-")

        target_repo = REPO_BY_LICENSE.get(license_val, REPO_BY_LICENSE["CC-BY-4.0"])

        flags_path = session_dir / "review-flags.json"
        review_flags = json.loads(flags_path.read_text()) if flags_path.exists() else []

        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        flag_lines = []
        for f in review_flags:
            icon = "🔴" if f["severity"] == "blocking" else "🟡"
            flag_lines.append(f"- {icon} **{f['field']}**: {f['reason']}")
        flags_md = "\n".join(flag_lines) if flag_lines else "- No flags"

        review_notes = f"""# Review notes

**Submitted:** {now}
**License:** {license_val}

## Automated review flags
{flags_md}

## Reviewer checklist
- [ ] Science looks sound
- [ ] Figures verified
- [ ] Ready to merge
"""

        g = Github(os.environ["GITHUB_TOKEN"])
        repo = g.get_repo(target_repo)

        base_branch = repo.get_branch(repo.default_branch)
        base_sha = base_branch.commit.sha
        branch_name = f"devnote/{devnote_id}"

        try:
            repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)
        except GithubException as e:
            if "already exists" in str(e).lower() or e.status == 422:
                return jsonify({"error": f"A review branch for this DevNote already exists. Check for an open PR or merge/close it before resubmitting."}), 409
            raise

        try:
            repo.get_contents(devnote_id, ref=repo.default_branch)
            is_revision = True
        except GithubException:
            is_revision = False

        def upsert_file(path, message, content, branch):
            try:
                existing = repo.get_contents(path, ref=branch)
                repo.update_file(path=path, message=message, content=content,
                                 sha=existing.sha, branch=branch)
            except GithubException:
                repo.create_file(path=path, message=message, content=content,
                                 branch=branch)

        for p in sorted(session_dir.rglob("*")):
            if not p.is_file():
                continue
            if p.name.startswith(".") or "__pycache__" in str(p):
                continue
            if p.name in SKIP_NAMES or p.suffix.lower() in SKIP_EXTS:
                continue
            rel = p.relative_to(session_dir)
            upsert_file(
                path=f"{devnote_id}/{rel}",
                message=f"Add {rel}",
                content=p.read_bytes(),
                branch=branch_name,
            )

        upsert_file(
            path=f"{devnote_id}/review-notes.md",
            message="Add review notes",
            content=review_notes.encode(),
            branch=branch_name,
        )

        authors = fm.get("authors", [])
        author_names = []
        if isinstance(authors, list):
            for a in authors:
                if isinstance(a, dict) and "name" in a:
                    author_names.append(a["name"])
                elif isinstance(a, str):
                    author_names.append(a)
        author_str = ", ".join(author_names) if author_names else "Unknown"

        pr_body = f"Submitted via Nucleus DevNote web app.\n\n**License:** {license_val}\n**Authors:** {author_str}"
        if is_revision:
            pr_body += "\n\n⚠️ **Revision** — a DevNote with this ID already exists in the repo. This submission may supersede a previous version. Review carefully before merging."

        pr = repo.create_pull(
            title=title,
            body=pr_body,
            head=branch_name,
            base=repo.default_branch,
        )

        return jsonify({"pr_url": pr.html_url})

    except Exception as e:
        app.logger.exception("Error in /publish")
        return jsonify({"error": str(e)}), 500


@app.route("/session/<session_id>/files/<path:filename>")
def session_file(session_id, filename):
    if not re.match(r"^[0-9a-f\-]{36}$", session_id):
        abort(400)
    session_dir = Path(f"/tmp/{session_id}")
    if not session_dir.exists():
        abort(404)
    return send_from_directory(session_dir, filename)


@app.route("/download/<session_id>")
def download(session_id):
    if not re.match(r"^[0-9a-f\-]{36}$", session_id):
        abort(400)
    session_dir = Path(f"/tmp/{session_id}")
    if not session_dir.exists():
        abort(404)
    zip_out = Path(f"/tmp/{session_id}-devnote-draft.zip")
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(session_dir.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(session_dir))
    return send_file(str(zip_out), as_attachment=True, download_name="devnote-draft.zip")


if __name__ == "__main__":
    app.run(debug=True)
