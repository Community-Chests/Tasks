import os
import json
import time
import requests

# =========================
# CONFIG (edit only if needed)
# =========================
ORG = "Community-Chests"
REPO = "Tasks"
PROJECT_NUMBER = 1  # from https://github.com/orgs/Community-Chests/projects/1

JSON_PATH = "data/social_media_tasks.json"

# Status values must match your Project Status options exactly (case-insensitive match).
DEFAULT_PROJECT_STATUS = os.getenv("PROJECT_STATUS", "Backlog")  # or "Ready"
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")


# =========================
# AUTH
# =========================
TOKEN = os.getenv("GITHUB_TOKEN")
if not TOKEN:
    raise SystemExit("Missing GITHUB_TOKEN env var. Set it before running.")

REST_HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
}

GQL_HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
}

REST = "https://api.github.com"
GQL = "https://api.github.com/graphql"


# =========================
# HELPERS
# =========================
def rest_get(url):
    r = requests.get(url, headers=REST_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

def rest_post(url, payload):
    r = requests.post(url, headers=REST_HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def gql(query, variables=None):
    r = requests.post(GQL, headers=GQL_HEADERS, json={"query": query, "variables": variables or {}}, timeout=30)
    r.raise_for_status()
    out = r.json()
    if "errors" in out:
        raise RuntimeError(out["errors"])
    return out["data"]

def load_tasks():
    with open(JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def build_task_ids(data):
    """
    Create stable sequential IDs from the JSON ordering.
    Example: SM-001, SM-002...
    """
    prefix = data.get("meta", {}).get("id_prefix", "SM")
    tasks = []
    counter = 1
    for section in data.get("sections", []):
        sec_name = section["name"]
        for task in section.get("tasks", []):
            tid = f"{prefix}-{counter:03d}"
            tasks.append((tid, sec_name, task))
            counter += 1
    return tasks

def issue_exists(task_id):
    """
    Checks if an issue already exists by searching the repo for the task ID in the title.
    """
    q = f'repo:{ORG}/{REPO} is:issue "{task_id}" in:title'
    url = f"{REST}/search/issues?q={requests.utils.quote(q)}"
    data = rest_get(url)
    return data.get("total_count", 0) > 0

def create_issue(title, body, label):
    if DRY_RUN:
        print(f"[DRY RUN] Would create issue: {title}")
        return None

    payload = {
        "title": title,
        "body": body,
        "labels": [label],
    }
    url = f"{REST}/repos/{ORG}/{REPO}/issues"
    created = rest_post(url, payload)
    return created["node_id"], created["number"], created["html_url"]

def get_project_node_id_and_status_field():
    """
    Get ProjectV2 node id, and the 'Status' single-select field + options.
    """
    query = """
    query($org:String!, $num:Int!) {
      organization(login:$org) {
        projectV2(number:$num) {
          id
          title
          fields(first:50) {
            nodes {
              ... on ProjectV2SingleSelectField {
                id
                name
                options { id name }
              }
              ... on ProjectV2FieldCommon {
                id
                name
              }
            }
          }
        }
      }
    }
    """
    data = gql(query, {"org": ORG, "num": PROJECT_NUMBER})
    proj = data["organization"]["projectV2"]
    project_id = proj["id"]

    status_field = None
    for f in proj["fields"]["nodes"]:
        if f.get("name", "").lower() == "status" and "options" in f:
            status_field = f
            break

    return project_id, proj["title"], status_field

def add_to_project(project_id, content_node_id):
    """
    Add issue (content) to project.
    Returns project item id.
    """
    mutation = """
    mutation($projectId:ID!, $contentId:ID!) {
      addProjectV2ItemById(input:{projectId:$projectId, contentId:$contentId}) {
        item { id }
      }
    }
    """
    if DRY_RUN:
        print("[DRY RUN] Would add issue to project")
        return None

    data = gql(mutation, {"projectId": project_id, "contentId": content_node_id})
    return data["addProjectV2ItemById"]["item"]["id"]

def set_project_status(project_id, item_id, status_field, status_value_name):
    if not status_field:
        print("WARNING: No Status field found on project. Skipping status set.")
        return

    option_id = None
    for opt in status_field.get("options", []):
        if opt["name"].strip().lower() == status_value_name.strip().lower():
            option_id = opt["id"]
            break

    if not option_id:
        available = [o["name"] for o in status_field.get("options", [])]
        raise RuntimeError(f"Status value '{status_value_name}' not found. Available: {available}")

    mutation = """
    mutation($projectId:ID!, $itemId:ID!, $fieldId:ID!, $optionId:String!) {
      updateProjectV2ItemFieldValue(input:{
        projectId:$projectId,
        itemId:$itemId,
        fieldId:$fieldId,
        value:{ singleSelectOptionId:$optionId }
      }) { projectV2Item { id } }
    }
    """

    if DRY_RUN:
        print(f"[DRY RUN] Would set Status={status_value_name}")
        return

    gql(mutation, {
        "projectId": project_id,
        "itemId": item_id,
        "fieldId": status_field["id"],
        "optionId": option_id,
    })

def main():
    data = load_tasks()
    label = data.get("meta", {}).get("label", "Social Media Tasks")
    source = data.get("meta", {}).get("source", "Unknown source")

    project_id, project_title, status_field = get_project_node_id_and_status_field()
    print(f"Project: {project_title} (#{PROJECT_NUMBER})")
    print(f"Repo: {ORG}/{REPO}")
    print(f"Label: {label}")
    print(f"Default Status: {DEFAULT_PROJECT_STATUS}")
    print(f"DRY_RUN: {DRY_RUN}\n")

    tasks = build_task_ids(data)
    created_count = 0
    skipped_count = 0

    for task_id, section_name, task_text in tasks:
        if issue_exists(task_id):
            print(f"SKIP (exists): {task_id} {task_text}")
            skipped_count += 1
            continue

        title = f"[{task_id}] {task_text}"
        body = (
            f"**Task ID:** {task_id}\n"
            f"**Section:** {section_name}\n"
            f"**Source:** {source}\n\n"
            f"### What this is\n"
            f"{task_text}\n\n"
            f"### Notes\n"
            f"- Auto-generated from `{JSON_PATH}`\n"
            f"- Add details, links, and acceptance criteria as needed.\n"
        )

        result = create_issue(title, body, label)
        if result is None and DRY_RUN:
            created_count += 1
            continue

        content_node_id, number, url = result
        item_id = add_to_project(project_id, content_node_id)
        if item_id:
            set_project_status(project_id, item_id, status_field, DEFAULT_PROJECT_STATUS)

        print(f"CREATED: #{number} {url}")
        created_count += 1
        time.sleep(0.4)  # gentle rate limiting

    print(f"\nDone. Created: {created_count}, Skipped (duplicates): {skipped_count}")

if __name__ == "__main__":
    main()
