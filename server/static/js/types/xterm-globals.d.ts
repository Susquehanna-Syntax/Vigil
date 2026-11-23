// The two vendored terminal globals vigil-terminal.js constructs. They are not
// npm packages (no dependency in server/package.json): loadTerminalAssets() in
// vigil-host-cards.js appends the /static/js/vendor/ xterm scripts at runtime, and those
// UMD builds publish `window.Terminal` and `window.FitAddon`. Sprint M6: this
// declares the minimum surface vigil-terminal.js touches, so its members are
// typed rather than every read falling through an `any`. Ambient declarations,
// like the rest of types/: no import or export anywhere in this file.

/** One row of the buffer, as vigil-terminal.js reads it. */
interface XtermBufferLine {
  isWrapped: boolean;
  length: number;
  translateToString(trimRight?: boolean, startColumn?: number, endColumn?: number): string;
}

/** A vendored xterm.js Terminal (window.Terminal). */
interface XtermTerminal {
  rows: number;
  cols: number;
  element: HTMLElement | null;
  textarea: HTMLTextAreaElement | null;
  buffer: {
    active: {
      /** Viewport row of buffer row y; negative y is the scrollback. */
      readonly viewportY: number;
      getLine(y: number): XtermBufferLine | undefined;
    };
  };
  markers: {
    registerMarker(cursorYOffset: number): { line: number; dispose(): void };
    clearMarker(marker: { line: number; dispose(): void }): void;
    /** Kind 0 is REGULAR, the kind vigil-terminal.js marks with. */
    getMarkers(type?: number): { line: number }[];
    clearList(type?: number): void;
  };
  parser: {
    registerOscHandler(ident: number, callback: (data: string) => boolean): { dispose(): void };
  };
  unicode: {
    activeVersion: string;
    versions: Record<string, unknown>;
    version: string;
  };
  open(container: HTMLElement): void;
  write(data: string | Uint8Array, callback?: () => void): void;
  writeln(data: string | Uint8Array, callback?: () => void): void;
  resize(cols: number, rows: number): void;
  focus(): void;
  dispose(): void;
  loadAddon(addon: XtermFitAddon): void;
  onResize(listener: (size: { cols: number; rows: number }) => unknown): { dispose(): void };
  onWriteParsed(listener: () => unknown): { dispose(): void };
  onBinary(listener: (data: string) => unknown): { dispose(): void };
  onData(listener: (data: string) => unknown): { dispose(): void };
  onLineFeed(listener: () => unknown): { dispose(): void };
  onKey(listener: (event: { key: string; domEvent: KeyboardEvent }) => unknown): { dispose(): void };
}

/** A vendored @xterm/addon-fit FitAddon (window.FitAddon.FitAddon). */
interface XtermFitAddon {
  activate?(terminal: XtermTerminal): void;
  dispose?(): void;
  fit(): void;
}

/** Defined by static/js/vendor/xterm.js (UMD build, publishes the global). */
declare const Terminal: new (opts: object) => XtermTerminal;

/** Defined by static/js/vendor/xterm-addon-fit.js (UMD build, publishes window.FitAddon). */
interface Window {
  FitAddon: { FitAddon: new () => XtermFitAddon };
}
