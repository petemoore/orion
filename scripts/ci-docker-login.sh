#!/bin/bash -exu

# Exit because PRs do not have access to secrets.
# The MonorepoManager executes a PR build though but does prevent a push for PRs.
if [ "$TRAVIS_PULL_REQUEST" != "false" ]; then
 exit 0
fi

# Pass the encrypted DOCKER_PASS to the `docker` client. Encrypted in the build logs.
# We perform a `docker logout` after the session in Travis ends.
echo "$DOCKER_PASS" | docker login -u "$DOCKER_USER" --password-stdin
