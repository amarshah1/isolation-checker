# isolation-checker

A first prototype for checking isolation between AI agents that share
infrastructure. It is motivated by the METR investigation of the OpenAI /
Hugging Face incident (August 2026), in which agents that were meant to be
isolated used a shared Artifactory package store as a message board (folder
names, properties, cached remote names) and as a relay to the internet.

It has two checks and two tools for testing the model against a real server:

| Command | Question it answers |
|---|---|
| `python3 -m checker ni` | Can anything agent A does change what agent B observes through the shared service? (non-interference) |
| `python3 -m checker reach` | Can an agent reach the internet, another agent, or shared state, counting services that make requests for their callers? |
| `replay/replay.py` | Is each leak the checker found real on an actual server? |
| `replay/conformance.py` | Does the model answer like the real server on every short trace? |

The checker itself needs only Python 3.10+, with no dependencies.

## Quick start

```bash
python3 -m checker demo                                   # everything below, plus a summary
python3 -m checker ni --deployment direct                 # the incident's setup: leaks
python3 -m checker ni --deployment naive-proxy            # per-agent folders, still leaks
python3 -m checker ni --deployment isolating-proxy --depth 4   # no leaks found
python3 -m checker reach configs/incident.json
python3 -m unittest discover -s tests
```

Against a real WebDAV server (wsgidav, installed into `.venv` on first run):

```bash
./replay/serve_webdav.sh &                                # 127.0.0.1:8080, anonymous, localhost only
python3 -m checker ni --deployment direct --json out/direct.json
python3 replay/replay.py out/direct.json                  # confirm each leak on the server
python3 replay/conformance.py --depth 2                   # where is the model wrong?
```

## How the non-interference check works

`checker/model.py` is a small model of the shared service: folders, files,
properties, a remote-repository cache, and a shared capacity limit. Each
operation (`mkdir`, `put`, `get`, `list`, `setprop`, `getprop`, `fetch`) is a
pure function of the state.

`checker/ni.py` tries every interleaving of both agents' operations up to a
bound (default 3 steps). For each trace it asks: does B see the same thing as
in the same trace with all of A's steps deleted? If not, A can choose between
the two worlds and B can tell which one it is in, which is a channel. It
reports the shortest example of each kind of leak, keeping only leaks where
every one of A's steps actually affects what B sees.

There are three deployments of the same service:

| Deployment | What it does | Result |
|---|---|---|
| `direct` | agents share one namespace (the incident) | leaks through the namespace, remote cache, and capacity |
| `naive-proxy` | per-agent folders, but the remote cache and capacity are still shared | leaks through the remote cache and capacity |
| `isolating-proxy` | per-agent folders, remote-cache keys, and capacity limits | no leak up to depth 4 |

The naive proxy is the important case: it looks isolated, and a reviewer
might accept it, but the checker finds that one agent filling the shared
capacity or warming the shared remote cache is visible to the other.

## How the reachability check works

`checker/reach.py` reads a JSON config of nodes and allowed connections.
Packet-level analysis (what Tiros / VPC Reachability Analyzer check) continues
only through routers. Relay-aware analysis also continues through services
marked `"relay": true`, which make requests on behalf of their callers, like a
package store's remote repositories.

| Config | Packet-level | Relay-aware |
|---|---|---|
| `incident.json` | no internet access; shared state | **reaches the internet** via the package store; shared state |
| `proxy-only.json` | pass | **reaches the internet** through the proxy and the store |
| `fixed.json` | pass | pass |

`incident.json` passing the packet-level internet check while failing the
relay-aware one is the incident's pattern (METR report, fn. 68: direct access
to Hugging Face was blocked, but agents "had already found a way to access the
full internet via Artifactory").

## Results of the first run (against wsgidav 4.4.0)

- **Replay:** 15 of the 19 leaks found in `direct` were confirmed on the
  server. The other 4 need `fetch`, which a plain WebDAV server does not
  have. On every replayed step, the server answered exactly as the model
  predicted.
- **Conformance:** across all 18,278 traces up to 3 steps, the server
  disagreed with the model on one kind of step. The model says `get` on a
  folder returns 404, but the server returns 403. That hides a real leak: B
  can tell whether A created folder `x` by calling `get(x)`. The checker did
  not report it because the model was wrong, and replay could not catch it
  because replay only tests leaks the model already found. The model is left
  unchanged so the finding stays reproducible.

## Limits

- **Bounded search.** "No leak" means none in traces up to the depth tried,
  in a universe of two agents and two names. It is not a proof. The next step
  is a symbolic (Z3) version, then a Verus proof of the proxy (Spec 7).
- **The model can be wrong about the service.** `replay.py` confirms leaks the
  model found, but cannot find leaks the model misses. `conformance.py` looks
  for those by comparing every short trace with the real server.
- **Timing is not modeled.** A cache hit that is faster than a miss is a
  channel this checker does not see.
- **The replay server is wsgidav, not Artifactory.** It speaks the same WebDAV
  operations (MKCOL, PROPFIND, PROPPATCH) but has no remote repositories or
  capacity limit, so `fetch` and quota leaks are skipped. Artifactory OSS in
  Docker is the next target.
- **The proxies exist only in the model.** There is no real proxy to replay
  against yet.

## Layout

```
checker/model.py      the shared-service model and the three deployments
checker/ni.py         the two-worlds (non-interference) search
checker/reach.py      relay-aware reachability
checker/__main__.py   command line
configs/              network configs for the reachability check
replay/replay.py      replay leaks against a WebDAV server
replay/conformance.py compare the model with a WebDAV server
replay/serve_webdav.sh, wsgidav.yaml   local test server
tests/                unit tests
```
