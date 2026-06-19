#!/usr/bin/env python3
"""
Sync a Folder to GitHub
=======================
Compares local files to those already at GitHub and uploads any files
that are new OR have changed, all in a single commit. Unchanged files
are skipped. No matter how many files, it's one commit.

ALL files are synced, regardless of type or extension. The only things
left out are deliberate safety exclusions (see EXCLUDE_DIRS / EXCLUDE_NAMES
below): the local .git directory, and the .github_token / .github_url
sidecar files. The token exclusion matters — it keeps your credentials
from being uploaded.

"Changed" is detected by content, not by date modified: the script
computes each local file's Git blob SHA-1 and compares it to the SHA
already stored in the repository. If they differ, the file is re-uploaded.

Empty repositories are supported: if the target repo has no commits yet
(neither 'main' nor 'master' exists), the script creates an initial commit
via the Contents API to bring 'main' into existence, then syncs the rest
normally. See the note in upload_files() about why this two-step dance is
necessary.

GitHub Pages is enabled automatically: after a successful sync the script
ensures Pages is turned on (deploy from the synced branch, root folder) so
the site is served publicly. If it's already on, nothing changes; if it
can't be enabled (e.g. token permissions), the script just warns and tells
you how to do it by hand — the sync itself still counts as successful.

Usage:
    python sync_github.py

The script will ask you for:
  1. Your GitHub Personal Access Token
  2. Your repository (e.g., DavidKurtRose/AjijicTreeAtlas)
  3. The local folder containing the files to sync

First-time setup — creating a Personal Access Token:
  1. Go to https://github.com/settings/tokens
  2. Click "Generate new token" → "Generate new token (classic)"
  3. Give it a name like "Atlas Sync"
  4. Check the "repo" scope (full control of private repositories)
  5. Click "Generate token" at the bottom
  6. Copy the token — you won't see it again!

The token is remembered: if a file called .github_token exists in this
folder it's read automatically, otherwise the script prompts for it and
offers to save it there for next time.

The repository is remembered too: the first time you run it, it asks for
your repository URL and saves it to a file called .github_url in this
folder. After that it reads the repository from .github_url automatically.
"""

import os
import sys
import json
import base64
import hashlib
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

# We sync ALL files regardless of type or extension. The only things we
# deliberately leave out are listed below — and these exclusions are
# important, not cosmetic:
#   EXCLUDE_DIRS  — directories whose contents must never be uploaded.
#                   '.git' is the local Git repository's internals: hundreds
#                   of opaque objects that are meaningless (and huge) as repo
#                   content.
#   EXCLUDE_NAMES — specific filenames that must never be uploaded.
#                   '.github_token' holds YOUR Personal Access Token — a
#                   secret. Uploading it would publish your credentials, so
#                   it is skipped unconditionally. '.github_url' is just a
#                   local pointer and pointless to upload.
EXCLUDE_DIRS = {'.git'}
EXCLUDE_NAMES = {'.github_token', '.github_url'}


def api_request(url, token, method='GET', data=None, suppress_codes=None):
    """Make a GitHub API request and return parsed JSON.

    suppress_codes: an optional collection of HTTP status codes for which
    we should NOT print an error message before re-raising. This lets
    callers probe for things that may legitimately be absent (e.g. a
    branch that doesn't exist yet) without spewing scary 404 noise."""
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json',
        'User-Agent': 'AtlasSync/1.0',
    }
    if data is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(data).encode('utf-8')
    else:
        body = None

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if not (suppress_codes and e.code in suppress_codes):
            error_body = e.read().decode('utf-8', errors='replace')
            print(f"\n  API error {e.code}: {error_body[:300]}")
        raise


def get_ref(token, repo, branch):
    """Return the ref data for a branch head, or None if that branch does
    not exist. GitHub signals 'no such branch' two different ways here:
      - 404 when the repo has commits but not this particular branch, and
      - 409 'Git Repository is empty' when the repo has no commits at all.
    Both mean the same thing for our purposes, so we treat them alike."""
    base = 'https://api.github.com'
    try:
        return api_request(
            f'{base}/repos/{repo}/git/ref/heads/{branch}',
            token,
            suppress_codes=(404, 409),
        )
    except urllib.error.HTTPError as e:
        if e.code in (404, 409):
            return None
        raise


def create_initial_file(token, repo, rel_path, file_bytes, message):
    """Create a single file using the Contents API.

    Why this exists: the low-level Git Data API (blobs/trees/commits) that
    the rest of this script uses CANNOT write to a repository that has zero
    commits — GitHub rejects object creation on an empty repo. The Contents
    API, by contrast, happily creates the first commit and brings the
    repository's default branch into being. So we use it once, only to
    bootstrap an empty repo, and then hand off to the normal flow.

    We deliberately do NOT pass a 'branch' parameter, letting GitHub create
    the repo's configured default branch (normally 'main'). The caller
    re-detects the branch name afterwards."""
    base = 'https://api.github.com'
    b64 = base64.b64encode(file_bytes).decode('ascii')
    # Path goes in the URL here (unlike tree items, which carry it in JSON),
    # so it must be percent-encoded — but keep '/' as the path separator.
    encoded_path = urllib.parse.quote(rel_path, safe='/')
    return api_request(
        f'{base}/repos/{repo}/contents/{encoded_path}',
        token,
        method='PUT',
        data={'message': message, 'content': b64},
    )


def ensure_pages_enabled(token, repo, branch, path='/'):
    """Make sure GitHub Pages is turned on for the repo, serving the site
    from `branch` at `path` ('/' = repo root). Safe to call every run: if
    Pages is already on, it just reports the existing site.

    Requires a token with 'repo' scope and admin rights on the repo (you
    have both for repos you own). A failure here is deliberately NON-fatal:
    the media sync has already succeeded by this point, so we warn and tell
    you how to flip the switch by hand rather than crashing."""
    base = 'https://api.github.com'
    print("\n  Checking GitHub Pages...")
    try:
        # Already enabled? GET returns 404 when Pages has never been set up.
        try:
            info = api_request(
                f'{base}/repos/{repo}/pages', token, suppress_codes=(404,)
            )
            url = info.get('html_url') if isinstance(info, dict) else None
            print(f"  Pages is already enabled. Live at: {url or '(building...)'}")
            return
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise

        # Not set up yet — create the Pages site (deploy from a branch).
        print(f"  Enabling Pages: deploy from branch '{branch}', folder '{path}'...")
        info = api_request(
            f'{base}/repos/{repo}/pages',
            token,
            method='POST',
            data={'source': {'branch': branch, 'path': path}},
        )
        url = info.get('html_url') if isinstance(info, dict) else None
        print("  ✓ Pages enabled. It can take a minute or two to go live.")
        if url:
            print(f"  Live at: {url}")
    except urllib.error.HTTPError as e:
        print(f"  ⚠ Could not enable Pages automatically (HTTP {e.code}).")
        print("    Enable it by hand: repo Settings → Pages → Source")
        print(f"    'Deploy from a branch', branch '{branch}', folder '{path}'.")
    except Exception as ex:
        print(f"  ⚠ Could not enable Pages automatically: {ex}")
        print("    Enable it by hand under repo Settings → Pages.")


def git_blob_sha(filepath):
    """Compute the Git blob SHA-1 for a file, matching the value GitHub
    stores for each blob. Reads the file in chunks so large files
    don't all sit in memory at once."""
    filepath = Path(filepath)
    size = filepath.stat().st_size
    h = hashlib.sha1()
    h.update(f'blob {size}\0'.encode('utf-8'))
    with open(filepath, 'rb') as fp:
        for chunk in iter(lambda: fp.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def get_token():
    """Get GitHub token from file or user input."""
    token_file = Path(__file__).parent / '.github_token'
    if token_file.exists():
        token = token_file.read_text().strip()
        if token:
            print(f"  Using token from {token_file}")
            return token

    print("  Enter your GitHub Personal Access Token.")
    print("  (It will not be displayed as you type.)")
    print()
    try:
        import getpass
        token = getpass.getpass("  Token: ").strip()
    except Exception:
        token = input("  Token: ").strip()

    if not token:
        print("  No token provided. Exiting.")
        sys.exit(1)

    save = input("  Save token for future use? (y/n): ").strip().lower()
    if save == 'y':
        token_file.write_text(token)
        print(f"  Token saved to {token_file}")

    return token


def normalize_repo(value):
    """Accept either 'owner/repo' or a full GitHub URL (https or SSH) and
    return the canonical 'owner/repo' form."""
    value = value.strip().strip('"').strip("'")
    if value.endswith('.git'):
        value = value[:-4]
    value = value.rstrip('/')
    if value.startswith('git@'):
        # git@github.com:owner/repo
        value = value.split(':', 1)[-1]
    elif 'github.com/' in value:
        value = value.split('github.com/', 1)[1]
    return value


def get_repo(token):
    """Get the repository (owner/repo) from .github_url if present, otherwise
    ask the user and save it to .github_url for next time."""
    url_file = Path(__file__).parent / '.github_url'
    if url_file.exists():
        raw = url_file.read_text().strip()
        if raw:
            repo = normalize_repo(raw)
            if '/' in repo:
                print(f"  Using repository from {url_file}: {repo}")
                return repo

    print()
    raw = input("  Repository URL or owner/repo\n"
                "  (e.g., https://github.com/DavidKurtRose/AjijicTreeAtlas): ").strip()
    repo = normalize_repo(raw)
    if '/' not in repo:
        print("  Repository must be a GitHub URL or in the format owner/repo-name")
        sys.exit(1)

    url_file.write_text(f'https://github.com/{repo}\n')
    print(f"  Saved repository to {url_file}")
    return repo


def collect_files(folder):
    """Collect ALL files under the folder, recursively, regardless of type
    or extension. The only omissions are the safety exclusions defined at the
    top of this file (EXCLUDE_DIRS / EXCLUDE_NAMES) — most importantly the
    .github_token file, so your access token is never uploaded.

    Other dotfiles (e.g. .nojekyll, .gitignore) ARE included, since those are
    often legitimately wanted in a repo."""
    folder = Path(folder)
    files = []
    for item in sorted(folder.rglob('*')):
        if not item.is_file():
            continue
        rel_parts = item.relative_to(folder).parts
        # Skip anything living inside an excluded directory (e.g. .git/...).
        if any(part in EXCLUDE_DIRS for part in rel_parts):
            continue
        # Skip specific sensitive/sidecar filenames wherever they appear.
        if item.name in EXCLUDE_NAMES:
            continue
        files.append(item)
    return files


def get_existing_files(token, repo, tree_sha):
    """Return a dict mapping file path -> blob SHA for everything already
    in the repository tree."""
    base = 'https://api.github.com'
    # recursive=1 returns all files in the tree, not just top-level
    tree_data = api_request(
        f'{base}/repos/{repo}/git/trees/{tree_sha}?recursive=1', token
    )
    existing = {}
    for item in tree_data.get('tree', []):
        if item['type'] == 'blob':
            existing[item['path']] = item['sha']
    return existing


def upload_files(token, repo, folder):
    """Sync all files to the GitHub repo in a single commit, uploading
    files that are new or whose content has changed."""
    base = 'https://api.github.com'
    folder = Path(folder)

    # 1. Collect files
    print("\n  Scanning folder for files...")
    files = collect_files(folder)
    if not files:
        print("  No files found. Nothing to sync.")
        return

    print(f"  Found {len(files)} files in local folder.")

    # 2. Find the target branch and its current state.
    #
    #    A brand-new, empty repository has NO branches yet — neither 'main'
    #    nor 'master' exists. The Git Data API we use below (blobs/trees/
    #    commits) cannot write to a repo with zero commits, so in that case
    #    we first lay down one file with the Contents API to create the
    #    initial commit (and the 'main' branch). After that the repo looks
    #    like any other non-empty repo and the rest of this function proceeds
    #    unchanged.
    print("  Getting repository info...")
    ref_data = get_ref(token, repo, 'main')
    branch = 'main'
    if ref_data is None:
        ref_data = get_ref(token, repo, 'master')
        if ref_data is not None:
            branch = 'master'

    if ref_data is None:
        # Empty repository: bootstrap it with an initial commit.
        print("  Repository is empty — creating an initial commit to start 'main'...")
        seed = files[0]
        seed_path = seed.relative_to(folder).as_posix()
        create_initial_file(
            token, repo, seed_path, seed.read_bytes(),
            message='Initial commit (atlas sync bootstrap)',
        )
        # The Contents API created the repo's default branch; re-detect it.
        ref_data = get_ref(token, repo, 'main')
        branch = 'main'
        if ref_data is None:
            ref_data = get_ref(token, repo, 'master')
            branch = 'master'
        if ref_data is None:
            print("  Could not confirm the initial branch after bootstrapping. Aborting.")
            sys.exit(1)
        print(f"  Created branch '{branch}' with initial file: {seed_path}")

    head_sha = ref_data['object']['sha']
    commit_data = api_request(f'{base}/repos/{repo}/git/commits/{head_sha}', token)
    base_tree_sha = commit_data['tree']['sha']
    print(f"  Current HEAD: {head_sha[:8]} (branch '{branch}')")

    # 3. Check what's already in the repo (path -> blob SHA)
    print("  Checking existing files in repository...")
    existing = get_existing_files(token, repo, base_tree_sha)
    print(f"  Found {len(existing)} files already in repository.")

    # 4. Compare by content: new files and changed files need uploading
    print("  Comparing local files to repository (by content)...")
    new_files = []
    changed_files = []
    unchanged = []
    for f in files:
        rel_path = f.relative_to(folder).as_posix()
        local_sha = git_blob_sha(f)
        repo_sha = existing.get(rel_path)
        if repo_sha is None:
            new_files.append(f)
        elif repo_sha != local_sha:
            changed_files.append(f)
        else:
            unchanged.append(rel_path)

    if unchanged:
        print(f"  {len(unchanged)} files unchanged — skipping.")
    if new_files:
        print(f"  {len(new_files)} new files.")
    if changed_files:
        print(f"  {len(changed_files)} changed files.")

    to_upload = new_files + changed_files
    if not to_upload:
        print("\n  ✓ Everything is already up to date. No files to upload.")
        # Still make sure Pages is on, in case it wasn't enabled before.
        ensure_pages_enabled(token, repo, branch)
        return

    total_size = sum(f.stat().st_size for f in to_upload)
    print(f"  {len(to_upload)} files to upload ({total_size / (1024*1024):.1f} MB)")
    print()

    # 5. Create blobs for each file to upload
    print(f"  Uploading {len(to_upload)} files...")
    tree_items = []
    for i, filepath in enumerate(to_upload, 1):
        rel_path = filepath.relative_to(folder).as_posix()
        file_bytes = filepath.read_bytes()
        b64_content = base64.b64encode(file_bytes).decode('ascii')

        size_kb = len(file_bytes) / 1024
        tag = 'changed' if filepath in changed_files else 'new'
        print(f"  [{i}/{len(to_upload)}] {rel_path} ({size_kb:.0f} KB, {tag})",
              end='', flush=True)

        blob_data = api_request(
            f'{base}/repos/{repo}/git/blobs',
            token,
            method='POST',
            data={
                'content': b64_content,
                'encoding': 'base64'
            }
        )
        blob_sha = blob_data['sha']
        print(f" ✓")

        tree_items.append({
            'path': rel_path,
            'mode': '100644',
            'type': 'blob',
            'sha': blob_sha
        })

    # 6. Create a new tree
    print("\n  Creating tree...")
    tree_data = api_request(
        f'{base}/repos/{repo}/git/trees',
        token,
        method='POST',
        data={
            'base_tree': base_tree_sha,
            'tree': tree_items
        }
    )
    new_tree_sha = tree_data['sha']

    # 7. Create a new commit
    print("  Creating commit...")
    n_new = len(new_files)
    n_changed = len(changed_files)
    parts = []
    if n_new:
        parts.append(f'{n_new} new')
    if n_changed:
        parts.append(f'{n_changed} changed')
    summary = ' and '.join(parts)
    new_commit = api_request(
        f'{base}/repos/{repo}/git/commits',
        token,
        method='POST',
        data={
            'message': f'Sync atlas media: {summary} file(s)',
            'tree': new_tree_sha,
            'parents': [head_sha]
        }
    )
    new_commit_sha = new_commit['sha']

    # 8. Update the branch reference to point at the new commit.
    #    We already know the branch name for certain (detected above), so
    #    there's no need to guess between main/master here anymore.
    print(f"  Updating branch '{branch}'...")
    api_request(
        f'{base}/repos/{repo}/git/refs/heads/{branch}',
        token,
        method='PATCH',
        data={'sha': new_commit_sha}
    )

    print(f"\n  ✓ Done! {summary} file(s) uploaded in a single commit.")
    print(f"  Commit: {new_commit_sha[:8]}")
    print(f"  Repository: https://github.com/{repo}")

    # 9. Make sure the site is actually being served.
    ensure_pages_enabled(token, repo, branch)


def main():
    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║   Sync a Folder to GitHub            ║")
    print("  ╚══════════════════════════════════════╝")
    print()

    # Get token
    token = get_token()

    # Verify token works
    print("\n  Verifying token...")
    try:
        user_data = api_request('https://api.github.com/user', token)
        print(f"  Authenticated as: {user_data['login']}")
    except Exception:
        print("  Token is invalid or expired. Please check and try again.")
        sys.exit(1)

    # Get repo (from .github_url or by asking, saving for next time)
    repo = get_repo(token)

    # Verify repo exists
    try:
        api_request(f'https://api.github.com/repos/{repo}', token)
        print(f"  Repository found: {repo}")
    except Exception:
        print(f"  Repository '{repo}' not found or not accessible.")
        sys.exit(1)

    # Get folder (defaults to the folder this script lives in)
    print()
    script_dir = Path(__file__).parent.resolve()
    folder = input(f"  Local folder containing files to sync\n"
                   f"  [press Enter for {script_dir}]: ").strip()
    folder = folder.strip('"').strip("'")  # Remove quotes if user wraps path
    if not folder:
        folder = str(script_dir)
    if not os.path.isdir(folder):
        print(f"  Folder not found: {folder}")
        sys.exit(1)

    # Confirm
    files = collect_files(folder)
    print(f"\n  Ready to sync {len(files)} files")
    print(f"  from: {os.path.abspath(folder)}")
    print(f"  to:   https://github.com/{repo}")
    confirm = input("\n  Proceed? (y/n): ").strip().lower()
    if confirm != 'y':
        print("  Cancelled.")
        sys.exit(0)

    upload_files(token, repo, folder)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        # Early exits (sys.exit) still pause so the window stays readable.
        pass
    except Exception:
        import traceback
        print("\n  An unexpected error occurred:\n")
        traceback.print_exc()
    finally:
        try:
            input("\n  Press Enter to close...")
        except EOFError:
            pass
