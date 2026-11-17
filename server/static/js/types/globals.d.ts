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

/** Defined in server/static/js/vigil-tasks.js (adds the author/created fields
 *  the community repo's PR policy requires). Read bare, like the const above. */
declare function _injectCommunityMetadata(yaml: string): string;

/** Defined in server/static/js/vigil-widgets.js. */
declare function openWidgetSettings(widgetId: string): Promise<void>;

/** Defined in server/static/js/vigil-deploy.js (jumps to Tasks with the host
 *  preselected). */
declare function openDeployForHost(hostId: string|number): void;

/** Defined in server/static/js/vigil-host-cards.js (two-step delete from a host
 *  card's action button, which is always a <button>). */
declare function deleteHost(hostId: string, hostname: string, btn: HTMLButtonElement): Promise<void>;

/** Defined in server/static/js/vigil-host-cards.js. It reads `dataset`,
 *  `classList` and `style` off the `.host-card` elements under `root`, so it is
 *  typed at that element; callers pass a widget body, and with no argument it
 *  falls back to the whole document itself. */
declare function refreshHostCards(root?: HTMLElement|null): Promise<void>;

/** Defined in server/static/js/vigil-host-cards.js (fill the OS chip of every
 *  `.host-os-logo[data-os]` under `root`; same reasoning as refreshHostCards). */
declare function _injectOsLogos(root?: HTMLElement|null): void;

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
declare function openPicker(opts: {type: 'task' | 'playbook' | 'machine' | 'tag' | 'rollout_target' | string, title: string,
  onSelect: (item: {key: string, [k: string]: any}) => void, allowAdd?: boolean,
  items?: object[], ineligible?: (item: {key: string, [k: string]: any}) => string|null|undefined}): Promise<void>;

/** Defined in server/static/js/vigil-host-cards.js (the pinned-hosts list it
 *  keeps in localStorage). */
declare function getPins(): {id: string, hostname: string, status: string}[];

/** Defined in server/static/js/vigil-host-cards.js (the pin chip strip above the
 *  host dropdown; vigil-monitor.js repaints it when the selected host changes). */
declare function renderPinBar(): void;

/** Defined in server/static/js/vigil-host-cards.js (the Connect menu a host card
 *  or the monitor header opens; `anchorEl` is where it hangs from). */
declare function openConnectMenu(hostId: string, hostname: string, anchorEl: HTMLElement): Promise<void>;

/** Defined in server/static/js/vigil-vulns.js (reload the Vulnerabilities page). */
declare function refreshVulns(): Promise<void>;

/** Defined in server/static/js/vigil-settings.js (reload the Account pane's TOTP status). */
declare function refreshTotpStatus(): Promise<void>;

/** Defined in server/static/js/vigil-host-cards.js (the two-line Jackil block a
 *  detail view shows when Jackil is configured). `stillCurrent` is the caller's
 *  staleness check; vigil-tasks.js calls it behind a typeof guard. */
declare function renderJackilRelated(
  section: HTMLElement|null, body: HTMLElement|null, kind: string, id: string|number,
  stillCurrent: () => boolean): Promise<void>;

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

/** Set by the inline config script in templates/base.html. `__pendingCommunityYaml`
 *  is the YAML vigil-tasks.js hands to the community-submit dialog, so its Copy
 *  button can copy what the GitHub link opened. */
interface Window {
  VIGIL_CONFIG: { timezone: string, timeFormat: string, username: string };
  __pendingCommunityYaml?: string;
}

/** The vendored GridStack (static/js/vendor/gridstack-all.js), a third-party library. */
declare const GridStack: any;

/** The vendored Chart.js UMD build (static/js/vendor/chart.umd.min.js), a
 *  third-party library loaded at runtime by the Reports page. Declared as `any`
 *  per the vendor rule: its config object is chart-kind-specific and this repo
 *  builds it in seven different shapes. */
declare const Chart: any;

/** The three handler fields VigilRdpTunnel's constructor initialises to null and
 *  that Guacamole.Client — plus vigil-rdp.js itself, right after `new` — set to
 *  callbacks afterwards. tsc types a classic function-as-constructor from the
 *  members its body assigns to `this`, so these are merged into that name to give
 *  both `tunnel.onstatechange = …` and the guarded callback calls their type;
 *  `null` is in the union because that is what the constructor stores. */
interface VigilRdpTunnel {
  onstatechange: ((state: number) => void) | null;
  oninstruction: ((opcode: string, stream: GuacamoleInputStream) => void) | null;
  onerror: ((status: {message?: string}) => void) | null;
}

/** A stream argument of the Guacamole protocol instruction callbacks
 *  (static/js/vendor/guacamole-common.js). vigil-rdp.js answers one `neg`
 *  instruction through it and hands the stream to nothing else, so this names
 *  that field alone. */
interface GuacamoleInputStream {
  /** set by the client for a "neg" instruction; Vigil answers it once. */
  onext: ((opcode: string, ...args: any[]) => void) | null;
}

/** The vendored Apache guacamole-common-js 1.5.0
 *  (static/js/vendor/guacamole-common.js), a third-party library loaded at
 *  runtime by loadRdpAssets() in vigil-host-cards.js. Declared as `any` per the
 *  vendor rule: the client, tunnel and display surface vigil-rdp.js drives is
 *  dynamic (per-layer display objects, protocol streams). */
declare const Guacamole: any;

/** Defined in server/static/js/vigil-pickers.js (the in-modal task editor;
 *  `opts.id` opens an existing definition, `opts.yaml` seeds a new one). */
declare function openTaskModal(opts?: {id?: string|number, yaml?: string, onSaved?: (def: any) => void}): void;

/** Defined in server/static/js/vigil-playbooks.js (the playbook editor modal;
 *  `data` prefills a new or duplicated playbook, null starts an empty one). */
declare function _openEditor(data: object|null, editingId: string|number|null): void;

/** Defined in server/static/js/vigil-hunts.js (a run with any hunt step). */
declare function _runHasHuntSteps(run: {tasks?: object[]} | null | undefined): boolean;

/** Defined in server/static/js/vigil-hunts.js (the hunt results page for a run). */
declare function openHuntResults(runId: any): Promise<void>;
