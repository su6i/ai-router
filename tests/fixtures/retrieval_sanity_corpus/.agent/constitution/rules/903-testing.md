# Testing Fixture

## Coverage Expectations

New logic needs a passing test before merge; a fix needs a regression test
that fails without the fix and passes with it.

## Flaky Test Policy

A test that fails intermittently gets quarantined immediately and reported,
never silently retried until green.
