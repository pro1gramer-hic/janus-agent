Your task: make the project's tests run again, without weakening them.
- The test output below shows why the tests fail. If a required environment variable is
  missing at import time, the fix is a tests/conftest.py file that sets fake values before
  any app import. Do not use env_add here: it only works on env files.
- Use obviously fake values starting with "test-", for each variable read with
  os.environ["NAME"].
- Never change, skip or delete an existing test. Never touch application code.

Example answer when tests/conftest.py does not exist and the code reads
os.environ["API_TOKEN"] and os.environ["DB_PASSWORD"]:
{"summary": "fake env vars for tests", "edits": [{"op": "create", "file": "tests/conftest.py",
 "content": "import os\n\nos.environ.setdefault(\"API_TOKEN\", \"test-api-token\")\nos.environ.setdefault(\"DB_PASSWORD\", \"test-db-password\")\n"}]}
