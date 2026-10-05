# Evaluation Plan

## 1. Docker image build

- Run on every pull request and push via `.github/workflows/ci.yaml`.
- Build with `docker build --tag bombsquad-server:ci .`.
- Pass when Docker exits successfully and produces the image. This validates the Dockerfile, copied server assets, and image-layer commands; it does not validate server startup.

## 2. Container startup smoke test

- Run the built image in an isolated environment without publishing ports; capture its logs and stop it after a short timeout.
- Pass when the config loads, the headless process launches, and logs contain `Server started` before the timeout. Fail if the container exits early or reports a startup/configuration exception.
- The current config requests a shared playlist from an external service. For a deterministic CI smoke test, use a dedicated test config with a fixed or inline playlist if supported, rather than relying on that service being available.
- Expected limitations: this test does not establish public reachability. A startup without published ports reports that the server is not joinable from the internet. Existing deprecation or shutdown-thread warnings should be tracked separately from startup failures.

## 3. Staging connectivity and gameplay

- Deploy the candidate image to an isolated staging host, publish the configured game port (UDP 43210; TCP 43210 only if required by the server), and allow it through the host and cloud firewalls.
- From an external BombSquad client, verify the server appears or can be joined directly, a player can join, and one short match starts and ends.
- Pass when the client stays connected through the match, the server logs no fatal errors, and the master-server reachability check succeeds.
- Use staging credentials and a non-production playlist. Shut the server down after the test and confirm it stops cleanly.

## 4. Feature regression checks

- In staging, verify the deployment's configured critical features (for example authentication, admin permissions, bans, and stats reporting) with dedicated test accounts.
- Pass each check against an explicit expected result, and review server logs for errors. Keep these checks separate from the image-build gate so external service or gameplay failures are diagnosable.

## Current evidence

- `docker build --tag bombsquad-server:ci .` completed successfully locally.
- A bounded `docker run --rm bombsquad-server:ci` loaded the config, launched the headless server, and logged `Server started`. The run could not verify public joinability because it published no game ports.