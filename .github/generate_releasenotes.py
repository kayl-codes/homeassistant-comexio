# MIT License — originally by Joakim Sørensen @ludeeus

import re
import sys

from github import Github

BODY = """
[![Downloads for this release](https://img.shields.io/github/downloads/kayl-codes/homeassistant-comexio/{version}/total.svg)](https://github.com/kayl-codes/homeassistant-comexio/releases/{version})

{changes}
"""

CHANGES = """
## Changes

{integration_changes}

"""

CHANGE = "- [{line}]({link}) @{author}\n"
NOCHANGE = "_No changes in this release._"
REPO_NAME = "kayl-codes/homeassistant-comexio"

_SKIP_MARKERS = (
    "flake",
    " workflow",
    " test",
    "docs",
    "dev debug",
    "Merge branch ",
    "Merge pull request ",
)

GITHUB = Github(sys.argv[2])


def new_commits(repo, sha):
    from datetime import datetime

    dateformat = "%a, %d %b %Y %H:%M:%S GMT"
    release_commit = repo.get_commit(sha)
    since = datetime.strptime(release_commit.last_modified, dateformat)
    commits = list(repo.get_commits(since=since))
    if len(commits) <= 1:
        return False
    return reversed(commits[:-1])


def last_integration_release(github, skip=True):
    repo = github.get_repo(REPO_NAME)
    tag_sha = None
    data = {}
    tags = list(repo.get_tags())
    reg = r"^v?\d+(\.\d+){0,2}$"
    if tags:
        for tag in tags:
            tag_name = tag.name
            if re.match(reg, tag_name):
                tag_sha = tag.commit.sha
                if skip:
                    skip = False
                    continue
                break
    data["tag_name"] = tag_name
    data["tag_sha"] = tag_sha
    return data


def get_integration_commits(github, skip=True):
    repo = github.get_repo(REPO_NAME)
    commits = new_commits(repo, last_integration_release(github, skip)["tag_sha"])
    if not commits:
        return NOCHANGE

    changes = ""
    for commit in commits:
        msg = repo.get_git_commit(commit.sha).message
        if any(marker in msg for marker in _SKIP_MARKERS):
            continue
        msg = msg.split("\n", 1)[0]
        ath = commit.author.login if commit.author else "Unknown"
        changes += CHANGE.format(line=msg, link=commit.html_url, author=ath)

    return changes


UPDATERELEASE = str(sys.argv[4])
REPO = GITHUB.get_repo(REPO_NAME)
if UPDATERELEASE == "yes":
    VERSION = str(sys.argv[6]).replace("refs/tags/", "")
    RELEASE = REPO.get_release(VERSION)
    RELEASE.update_release(
        name=f"Comexio {VERSION}",
        message=BODY.format(
            version=VERSION,
            changes=CHANGES.format(
                integration_changes=get_integration_commits(GITHUB),
            ),
        ),
    )
else:
    integration_changes = get_integration_commits(GITHUB, False)
    if integration_changes != NOCHANGE:
        VERSION = last_integration_release(GITHUB, False)["tag_name"]
        VERSION = f"{VERSION[:-1]}{int(VERSION[-1]) + 1}"
        REPO.create_issue(
            title=f"Create release {VERSION}?",
            labels=["New release"],
            assignee="kayl-codes",
            body=CHANGES.format(integration_changes=integration_changes),
        )
    else:
        print("Not enough changes for a release.")
