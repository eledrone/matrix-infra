#!/usr/bin/env python3
"""
Move the deployment's identifying values (domains, LAN/public IP) out of the
tracked config files into environment variables, so a public repo does not
name the deployment.

Run ON the server:

    cd ~/docker/matrix-infra && python3 scripts/hide-domains.py

Nothing is restarted. It writes .tmpl files, appends the new variables to
homeserver/.env, and then verifies that rendering each template reproduces the
current live file byte-for-byte. If anything reports DIFFERS, stop.

element-call/config.json becomes a template too, so afterwards the rendered
config.json is generated at deploy time and gitignored, like the others.
"""

import os
import re
import subprocess

BASE = os.path.expanduser("~/docker/matrix-infra/homeserver")
os.chdir(BASE)

# ---------------------------------------------------------------- detection
dendrite = open("config/dendrite.yaml.tmpl", encoding="utf-8").read()
compose = open("docker-compose.yml", encoding="utf-8").read()
coturn = open("coturn/turnserver.conf.tmpl", encoding="utf-8").read()

m = re.search(r"^\s*server_name:\s*(\S+)\s*$", dendrite, re.M)
if not m:
    raise SystemExit("could not find server_name in dendrite.yaml.tmpl")
MATRIX_DOMAIN = m.group(1).strip('"')

m = re.search(r"LIVEKIT_URL:\s*wss://([^/\s]+)/", compose)
if not m:
    raise SystemExit("could not find LIVEKIT_URL in docker-compose.yml")
CALL_DOMAIN = m.group(1)

m = re.search(r"^external-ip=([0-9.]+)/([0-9.]+)\s*$", coturn, re.M)
if not m:
    raise SystemExit("could not find external-ip in turnserver.conf.tmpl")
TURN_EXTERNAL_IP, TURN_LISTENING_IP = m.group(1), m.group(2)

env = {
    "MATRIX_DOMAIN": MATRIX_DOMAIN,
    "CALL_DOMAIN": CALL_DOMAIN,
    "TURN_EXTERNAL_IP": TURN_EXTERNAL_IP,
    "TURN_LISTENING_IP": TURN_LISTENING_IP,
}
print("=== detected ===")
for k, v in env.items():
    print("  %-20s %s" % (k, v))

# Longest first, so call.<domain> is not partially eaten by <domain>.
SUBS = sorted(
    [(CALL_DOMAIN, "${CALL_DOMAIN}"),
     (MATRIX_DOMAIN, "${MATRIX_DOMAIN}"),
     (TURN_EXTERNAL_IP, "${TURN_EXTERNAL_IP}"),
     (TURN_LISTENING_IP, "${TURN_LISTENING_IP}")],
    key=lambda p: -len(p[0]),
)


def parameterise(text):
    for literal, var in SUBS:
        text = text.replace(literal, var)
    return text


# ------------------------------------------------------------------ rewrite
print("\n=== rewriting ===")

# element-call/config.json is not yet a template; make it one.
if not os.path.exists("element-call/config.json.tmpl"):
    src = open("element-call/config.json", encoding="utf-8").read()
    open("element-call/config.json.tmpl", "w", encoding="utf-8").write(parameterise(src))
    print("  element-call/config.json      -> new template")

for path in ("config/dendrite.yaml.tmpl", "coturn/turnserver.conf.tmpl", "docker-compose.yml"):
    before = open(path, encoding="utf-8").read()
    after = parameterise(before)
    if before != after:
        open(path, "w", encoding="utf-8").write(after)
        n = sum(before.count(lit) for lit, _ in SUBS)
        print("  %-30s %d occurrence(s) parameterised" % (path, n))
    else:
        print("  %-30s nothing to change" % path)

# ---------------------------------------------------------------------- env
existing = {}
if os.path.exists(".env"):
    for line in open(".env", encoding="utf-8"):
        if "=" in line:
            k, v = line.rstrip("\n").split("=", 1)
            existing[k] = v
existing.update(env)
with open(".env", "w", encoding="utf-8") as f:
    for k in sorted(existing):
        f.write("%s=%s\n" % (k, existing[k]))
os.chmod(".env", 0o600)
print("\n  .env now holds %d variables" % len(existing))

# -------------------------------------------------------------------- verify
varlist = " ".join("$" + k for k in sorted(existing))
print("\n=== VERIFICATION: does envsubst(template) reproduce the live file? ===")
allok = True
for live, tmpl in [("config/dendrite.yaml", "config/dendrite.yaml.tmpl"),
                   ("livekit/livekit.yaml", "livekit/livekit.yaml.tmpl"),
                   ("coturn/turnserver.conf", "coturn/turnserver.conf.tmpl"),
                   ("element-call/config.json", "element-call/config.json.tmpl")]:
    with open(tmpl, "rb") as fh:
        r = subprocess.run(["envsubst", varlist], stdin=fh, capture_output=True,
                           env={**os.environ, **existing})
    identical = r.stdout == open(live, "rb").read()
    allok &= identical
    print("  %-30s %s" % (live, "IDENTICAL" if identical else "*** DIFFERS ***"))

print("\n=== compose still resolves? ===")
r = subprocess.run(["docker", "compose", "config"], capture_output=True, cwd=BASE)
print("  docker compose config:", "OK" if r.returncode == 0 else "FAILED\n" + r.stderr.decode()[:400])

print("\nresult: %s" % ("all verified - safe to commit"
                        if allok and r.returncode == 0 else "MISMATCH - do not deploy, report this"))
