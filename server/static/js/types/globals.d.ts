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

/** Defined in server/static/js/vigil-widgets.js. */
declare function openWidgetSettings(widgetId: string): Promise<void>;

/** Defined in server/static/js/vigil-widgets.js (widget kind -> renderer). */
declare const WIDGET_RENDERERS: Record<string, (body: Element, settings: object, widget?: object) => unknown>;

/** Defined in server/static/js/vigil-host-cards.js (the tag combobox of the open
 *  host drawer keeps its own copy of the host's tags; vigil-tags.js writes an
 *  entry into it from that page's datalist). */
declare const _detailTagState: { hostId: string|null, tags: string[] };

/** Defined in server/static/js/vigil-host-cards.js (adds one tag to the open
 *  host and saves). */
declare function addDetailTag(name: string): Promise<void>;

/** vigil-store.js reads this bare and vigil-apps.js defines it (line 594 there).
 *  They are separate classic scripts, so at run time each has its own global
 *  scope and only vigil-apps.js has the binding - but tsc puts every file in
 *  tsconfig.json `files` in ONE program, where a second top-level declaration
 *  of the same name is TS2451. Declaring it here would cause that; this comment
 *  is the only place left to say what vigil-store.js's read means. The list is
 *  apps/tasks/spec.py _APP_SOURCE_CHOICES, as test_policy_from_app.py pins. */

/** Defined in server/static/js/vigil-host-cards.js (the host detail drawer).
 *  Callers pass a rendered `.host-card`, or a detached element carrying the
 *  same dataset; only `dataset` is read. */
declare function openHostDetail(card: {dataset: {[k: string]: string | undefined}}): void;

/** Defined in server/static/js/vigil-pickers.js. `type: 'tag'` is a caller-side
 *  type: openPicker only fetches when there is no `items` array, so a caller may
 *  name any type as long as it supplies its own items. */
declare function openPicker(opts: {type: 'task' | 'playbook' | 'machine' | 'tag' | 'rollout_target', title: string,
  onSelect: (item: {key: string, [k: string]: any}) => void, allowAdd?: boolean,
  items?: object[], ineligible?: (item: {key: string, [k: string]: any}) => string|null|undefined}): Promise<void>;

/** Defined in server/static/js/vigil-tasks.js (the empty-state block a list
 *  page shows when it has nothing: a title, a sentence, and an optional action). */
declare function _buildEmptyState(title: string, desc: string): HTMLElement;


/** Defined in server/static/js/vigil-tasks.js (the run-detail modal). */
declare function openTaskDetail(runId: any): Promise<void>;

/** Defined in server/static/js/vigil-devices.js (the Inventory page's device
 *  modal; `id` is a dataset string, empty for "add a new one"). */
declare function openDeviceModal(id?: any): void;

/** Defined in server/static/js/vigil-devices.js (loads the unmanaged-device
 *  list the device modal reads its row out of). */
declare function loadDevices(): Promise<void>;

/** Defined in server/static/js/vigil-automations.js (the automation editor;
 *  it needs the whole row, not an id). */
declare function _openAutoEditor(a?: object|null): void;

/** Defined in server/static/js/vigil-ai.js. */
declare function suggestFixForVuln(finding: object): void;

/** Defined in server/static/js/vigil-ai.js (the Suggest Fix button on a firing
 *  alert; vigil-alerts.js calls it behind a typeof guard). `alertId` is the
 *  row id, which reaches the DOM as text and back as a dataset string. */
declare function suggestFixForAlert(alertId: any, context?: string): void;

/** Defined in server/static/js/vigil-ai.js. */
declare function suggestMitigationsForFixGroup(group: object): void;

/** Defined in server/static/js/vigil-tasks.js (the task editor; opens an
 *  existing definition, or a new one from `initialYaml`). */
declare function openDefinitionEditor(definitionId: any, initialYaml?: string|null): Promise<void>;

/** Defined in server/static/js/vigil-tasks.js (one risk badge, used by the
 *  task, playbook and Community cards). */
declare function riskBadgeHtml(risk: string|undefined|null): string;

/** Defined in server/static/js/vigil-playbooks.js (`id` comes from a dataset,
 *  so it is a string on every path that reaches it). */
declare function _startEdit(id: any): void;

/** Defined in server/static/js/vigil-policies.js. */
declare function openPolicyEditor(policyId: any): unknown;

/** Defined in server/static/js/vigil-policies.js (a policy for the app named
 *  in `appName`, pre-filled with the app/source pairs). */
declare function policyFromApp(appName: string, pairs: object[], opts?: object): Promise<void>;

/** Defined in server/static/js/vigil-monitor.js (`hostId` reaches it from a
 *  dataset or an API row, both of which the page treats as opaque). */
declare function selectMonitorHost(hostId: any): void;

/** Defined in server/static/js/vigil-containers.js (the Containers page's host
 *  picker; same opaque `hostId`). */
declare function selectContainersHost(hostId: any): void;

/** Defined in server/static/js/vigil-stacks.js (reloads that host's managed
 *  stacks into the Containers pane). */
declare function renderManagedStacks(hostId: any): Promise<void>;

/** Defined in server/static/js/vigil-stacks.js (the stack editor for `stackId`). */
declare function openStackEditor(stackId: any): Promise<void>;

/** Defined in server/static/js/vigil-deploy.js (the built-in task named `name`,
 *  host and inputs prefilled). */
declare function openUpdateContainer(hostId: any, containerName?: string): Promise<void>;

/** Defined in server/static/js/vigil-deploy.js. */
declare function openDeployModal(definitionId: string, prefill?: object): Promise<void>;

/** Defined in server/static/js/vigil-pickers.js: the declared-inputs form. */
declare function inputsFormMount(container: HTMLElement, inputs: any[], values?: Record<string, any>): void;
/** Defined in server/static/js/vigil-pickers.js: read + validate that form. */
declare function inputsFormRead(container: HTMLElement): { values: Record<string, any>, error: string };

/** Set by the inline config script in templates/base.html. */
interface Window {
  VIGIL_CONFIG: { timezone: string, timeFormat: string, username: string };
}

/** The vendored GridStack (static/js/vendor/gridstack-all.js), a third-party library. */
declare const GridStack: any;

/** Defined in server/static/js/vigil-hunts.js (a run with any hunt step). */
declare function _runHasHuntSteps(run: {tasks?: object[]} | null | undefined): boolean;

/** Defined in server/static/js/vigil-hunts.js (the hunt results page for a run). */
declare function openHuntResults(runId: any): Promise<void>;
