Your task: configuration and secrets.
- Move every hardcoded secret to an environment variable read with os.environ["NAME"]
  (use the suggested_env_var name). Remove the literal value and any comment that excuses it.
  Make sure the module imports os.
- Every variable read by the code must be in .env.example. Variables read with
  os.environ["NAME"] or os.getenv("NAME") without a default must also be in every deploy
  config listed below. Use env_add with the comment "set in the secret manager".
- When you move a secret, also add its key to .env.example and to every deploy config.
