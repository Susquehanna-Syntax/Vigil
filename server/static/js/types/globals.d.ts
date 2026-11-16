// Globals that server/static/js/vigil-utils.js and vigil-modal.js use but do
// not define themselves — each declaration names the file that defines it.
// Sprint M6: // @ts-check type checking, no build step.

/** Defined in server/static/js/vigil-session.js (top-level function
 *  declaration; sessionState.limit is 'idle' | 'max'). */
declare function _sessionLeave(reason: 'idle' | 'max'): void;
