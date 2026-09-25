import { WRITE_ACTIONS } from './travis.js';

// TRAVIS_ALLOW_WRITE="restart,cancel" opts into the matching write tools. Unset or empty
// keeps the server read-only; an unknown value is a startup error rather than silently ignored.
export function parseAllowedWrites(value) {
  const actions = (value ?? '')
    .split(',')
    .map((action) => action.trim().toLowerCase())
    .filter(Boolean);

  const unknown = actions.filter((action) => !WRITE_ACTIONS.includes(action));
  if (unknown.length > 0) {
    throw new Error(`TRAVIS_ALLOW_WRITE: unknown action(s) ${unknown.join(', ')} (allowed: ${WRITE_ACTIONS.join(', ')})`);
  }

  return [...new Set(actions)];
}
