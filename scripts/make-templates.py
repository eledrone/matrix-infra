#!/usr/bin/env python3
"""
One-time migration: move every secret out of the matrix config files into
environment variables, leaving `.tmpl` files that are safe to commit.

Run this ON the server, in ~/docker/matrix:

    python3 scripts/make-templates.py

It does NOT restart anything. Your containers keep running untouched.

What it produces:
  config/dendrite.yaml.tmpl        <- committed;  config/dendrite.yaml stays as the rendered artifact
  livekit/livekit.yaml.tmpl        <- committed
  coturn/turnserver.conf.tmpl      <- committed
  docker-compose.yml               <- edited in place to use ${VARS} (Compose interpolates natively)
  docker-compose.yml.orig          <- backup of the original, gitignored
  .env                             <- all secret values, mode 0600, gitignored

Then it VERIFIES that rendering each template reproduces your current live file
byte-for-byte. If any file reports DIFFERS, do not deploy - tell me and stop.
"""

import os
import re
import shutil
import subprocess

BASE = os.path.expanduser("~/docker/matrix-infra/homeserver")
os.chdir(BASE)

env = {}      # varname -> real secret value. Never printed.
notes = []


def sub_line(text, pattern, var, quoted_ok=True):
    """Replace the value captured as 'val' with ${var}, preserving original quoting."""
    m = re.search(pattern, text, re.M)
    if not m:
        return text, False
    val = m.group("val")
    inner, wrap = val, "%s"
    if quoted_ok and len(val) > 1 and val[0] == val[-1] and val[0] in "\"'":
        inner, wrap = val[1:-1], val[0] + "%s" + val[0]
    env[var] = inner
    repl = m.group("pre") + (wrap % ("${%s}" % var))
    return text[:m.start()] + repl + text[m.end("val"):], True


# ---------------------------------------------------------------- dendrite.yaml
out = open("config/dendrite.yaml", encoding="utf-8").read()
for key, var in [
    ("connection_string", "DENDRITE_DB_CONNECTION_STRING"),
    ("registration_shared_secret", "DENDRITE_REGISTRATION_SHARED_SECRET"),
    ("recaptcha_public_key", "RECAPTCHA_PUBLIC_KEY"),
    ("recaptcha_private_key", "RECAPTCHA_PRIVATE_KEY"),
    ("recaptcha_bypass_secret", "RECAPTCHA_BYPASS_SECRET"),
    ("turn_shared_secret", "TURN_SHARED_SECRET"),
]:
    out, ok = sub_line(out, r"^(?P<pre>[ \t]*%s:[ \t]*)(?P<val>\S.*?)[ \t]*$" % key, var)
    notes.append("  dendrite.yaml    %-32s %s" % (key, "templated" if ok else "NOT FOUND"))

# The metrics basic_auth password is the only uncommented bare `password:` key.
out, ok = sub_line(out, r"^(?P<pre>[ \t]+password:[ \t]*)(?P<val>\S.*?)[ \t]*$",
                   "DENDRITE_METRICS_PASSWORD")
notes.append("  dendrite.yaml    %-32s %s" % ("password (metrics)", "templated" if ok else "NOT FOUND"))
open("config/dendrite.yaml.tmpl", "w", encoding="utf-8").write(out)

# ---------------------------------------------------------------- livekit.yaml
src_lk = open("livekit/livekit.yaml", encoding="utf-8").read()
m = re.search(r"^(?P<pre>[ \t]+)(?P<kname>API\S+?)(?P<mid>:[ \t]*)(?P<val>\S+)[ \t]*$", src_lk, re.M)
if m:
    env["LIVEKIT_KEY"] = m.group("kname")
    env["LIVEKIT_SECRET"] = m.group("val")
    open("livekit/livekit.yaml.tmpl", "w", encoding="utf-8").write(
        src_lk[:m.start()] + m.group("pre") + "${LIVEKIT_KEY}" + m.group("mid")
        + "${LIVEKIT_SECRET}" + src_lk[m.end("val"):])
    notes.append("  livekit.yaml     %-32s templated" % "keys (api key + secret)")
else:
    notes.append("  livekit.yaml     %-32s NOT FOUND" % "keys")

# ------------------------------------------------------------ turnserver.conf
out_ct, ok = sub_line(open("coturn/turnserver.conf", encoding="utf-8").read(),
                      r"^(?P<pre>static-auth-secret=)(?P<val>\S+)[ \t]*$",
                      "COTURN_STATIC_AUTH_SECRET", quoted_ok=False)
open("coturn/turnserver.conf.tmpl", "w", encoding="utf-8").write(out_ct)
notes.append("  turnserver.conf  %-32s %s" % ("static-auth-secret", "templated" if ok else "NOT FOUND"))

# TURN auth only works if coturn's secret matches dendrite's turn_shared_secret.
same = env.get("COTURN_STATIC_AUTH_SECRET") == env.get("TURN_SHARED_SECRET")
notes.append("  >> coturn secret == dendrite turn_shared_secret : %s"
             % ("YES - collapsing to one variable" if same else "NO - keeping two variables"))
if same:
    t = open("coturn/turnserver.conf.tmpl", encoding="utf-8").read()
    open("coturn/turnserver.conf.tmpl", "w", encoding="utf-8").write(
        t.replace("${COTURN_STATIC_AUTH_SECRET}", "${TURN_SHARED_SECRET}"))
    del env["COTURN_STATIC_AUTH_SECRET"]

# ------------------------------------------------------------ docker-compose.yml
shutil.copy2("docker-compose.yml", "docker-compose.yml.orig")
out_dc = open("docker-compose.yml", encoding="utf-8").read()
for key in ("POSTGRES_PASSWORD", "LIVEKIT_KEY", "LIVEKIT_SECRET"):
    m = re.search(r"^(?P<pre>[ \t]*%s:[ \t]*)(?P<val>\S+)[ \t]*$" % key, out_dc, re.M)
    if not m:
        notes.append("  compose          %-32s NOT FOUND" % key)
        continue
    if env.get(key) is not None and env[key] != m.group("val"):
        notes.append("  !! compose %s differs from the value already found elsewhere" % key)
    env.setdefault(key, m.group("val"))
    out_dc = out_dc[:m.start()] + m.group("pre") + "${%s}" % key + out_dc[m.end("val"):]
    notes.append("  compose          %-32s templated" % key)
open("docker-compose.yml", "w", encoding="utf-8").write(out_dc)

# ----------------------------------------------------------------------- .env
# Keeps plain `docker compose up -d` working by hand, and acts as the fallback
# if the vault is ever unreachable at deploy time.
with open(".env", "w", encoding="utf-8") as f:
    for k in sorted(env):
        f.write("%s=%s\n" % (k, env[k]))
os.chmod(".env", 0o600)

# --------------------------------------------------------------------- verify
varlist = " ".join("$" + k for k in sorted(env))
print("\n=== templating ===")
print("\n".join(notes))
print("\n=== secrets moved into env (%d) ===" % len(env))
print("  " + "  ".join(sorted(env)))

print("\n=== VERIFICATION: does envsubst(template) reproduce the live file? ===")
allok = True
for live, tmpl in [("config/dendrite.yaml", "config/dendrite.yaml.tmpl"),
                   ("livekit/livekit.yaml", "livekit/livekit.yaml.tmpl"),
                   ("coturn/turnserver.conf", "coturn/turnserver.conf.tmpl")]:
    if not os.path.exists(tmpl):
        print("  %-28s SKIPPED (no template produced)" % live)
        allok = False
        continue
    with open(tmpl, "rb") as fh:
        r = subprocess.run(["envsubst", varlist], stdin=fh, capture_output=True,
                           env={**os.environ, **env})
    identical = r.stdout == open(live, "rb").read()
    allok &= identical
    print("  %-28s %s" % (live, "IDENTICAL" if identical else "*** DIFFERS ***"))

print("\nresult: %s" % ("all templates verified - safe to commit"
                        if allok else "MISMATCH - do not deploy, report this"))
print("\nnothing was restarted. `docker compose config` should still resolve; check with:")
print("  cd ~/docker/matrix && docker compose config >/dev/null && echo OK")
