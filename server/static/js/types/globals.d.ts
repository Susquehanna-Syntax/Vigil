// Globals that the checked files in server/tsconfig.json use but do not define
// themselves — each declaration names the file that defines it.
// Sprint M6: // @ts-check type checking, no build step.
//
// A name needs a declaration here only when its defining script is NOT in
// tsconfig.json's `files` list; tsc sees cross-file globals between the files
// it checks.

/** Defined in server/static/js/vigil-session.js (top-level function
 *  declaration; sessionState.limit is 'idle' | 'max'). */
declare function _sessionLeave(reason: 'idle' | 'max'): void;

/** Defined in server/static/js/vigil-playbooks.js (refreshes the playbook
 *  list; vigil-contentyaml.js calls it behind a typeof guard). */
declare function loadPlaybooks(): unknown;

/** Defined in server/static/js/vigil-automations.js (refreshes the automation
 *  list; vigil-contentyaml.js calls it behind a typeof guard). */
declare function loadAutomations(): unknown;

/** Defined in server/static/js/vigil-tasks.js. Top-level `const`, so a classic
 *  <script src> tag does not put it on window — vigil-contentyaml.js reads it
 *  bare and relies on base.html loading vigil-tasks.js first. */
declare const COMMUNITY_REPO_URL: string;

/** Defined in server/static/js/vigil-tasks.js (adds the author/created fields
 *  the community repo's PR policy requires). Read bare, like the const above. */
declare function _injectCommunityMetadata(yaml: string): string;

/** Defined in server/static/js/vigil-tasks.js (task state -> CSS colour). */
declare const _TASK_STATE_COLORS: Record<string, string>;

/** Defined in server/static/js/vigil-tasks.js (task state -> label). */
declare const TASK_STATE_LABELS: Record<string, string>;

/** Defined in server/static/js/vigil-runhistory.js (one row per step, escaped). */
declare function _stepResultsHtml(resultData: any, output: string | null | undefined): string;

/** Defined in server/static/js/vigil-widgets.js. */
declare function openWidgetSettings(widgetId: string): Promise<void>;

/** Defined in server/static/js/vigil-widgets.js (widget kind -> renderer). */
declare const WIDGET_RENDERERS: Record<string, (body: Element, settings: object, widget?: object) => unknown>;

/** Defined in server/static/js/vigil-pickers.js. */
declare function openPicker(opts: {type: 'task' | 'playbook' | 'machine', title: string,
  onSelect: (item: {key: string, [k: string]: any}) => void, allowAdd?: boolean}): Promise<void>;

/** Defined in server/static/js/vigil-ai.js. */
declare function suggestFixForVuln(finding: object): void;

/** Defined in server/static/js/vigil-ai.js. */
declare function suggestMitigationsForFixGroup(group: object): void;

/** Defined in server/static/js/vigil-deploy.js. */
declare function openDeployModal(definitionId: string, prefill?: object): Promise<void>;

/** The vendored GridStack (static/js/vendor/gridstack-all.js), a third-party library. */
declare const GridStack: any;
