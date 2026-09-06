# Deployment Fixture

## Release Checklist

Tag the release, build the container image, push it to the registry, and
roll it out to staging before production.

## Rollback

Roll back automatically if the health check fails shortly after rollout.
