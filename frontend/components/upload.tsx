"use client";
import { useRef, useState } from "react";
import { Upload } from "lucide-react";
import { Button, type ButtonProps } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export function UploadButton({ onFiles, accept = ".pdf,.png,.jpg,.jpeg,.tif,.tiff", multiple = false, children, testId, ...btn }:
  { onFiles: (f: File[]) => Promise<void> | void; accept?: string; multiple?: boolean; testId?: string } & ButtonProps) {
  const ref = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  return (
    <>
      <input ref={ref} type="file" hidden accept={accept} multiple={multiple} data-testid={testId}
        onChange={async (e) => {
          const files = Array.from(e.target.files || []);
          e.target.value = "";
          if (!files.length) return;
          setBusy(true);
          try { await onFiles(files); } finally { setBusy(false); }
        }} />
      <Button type="button" {...btn} disabled={busy || btn.disabled} onClick={() => ref.current?.click()}>
        <Upload />{busy ? "Uploading…" : children}
      </Button>
    </>
  );
}

export function DropZone({ onFiles, label, hint, testId }: { onFiles: (f: File[]) => Promise<void>; label: string; hint?: string; testId?: string }) {
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLInputElement>(null);
  async function handle(files: File[]) {
    if (!files.length) return;
    setBusy(true);
    try { await onFiles(files); } finally { setBusy(false); }
  }
  return (
    <div
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); handle(Array.from(e.dataTransfer.files)); }}
      onClick={() => ref.current?.click()}
      role="button" tabIndex={0}
      className={cn("flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed p-6 text-center transition-colors",
        over ? "border-accent bg-accent/5" : "border-line bg-white hover:bg-panel")}>
      <input ref={ref} type="file" hidden multiple accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff" data-testid={testId}
        onChange={(e) => { const f = Array.from(e.target.files || []); e.target.value = ""; handle(f); }} />
      <Upload className="mb-2 size-6 text-muted" />
      <div className="font-semibold">{busy ? "Scanning, hashing & validating…" : label}</div>
      {hint && <div className="mt-1 text-xs text-muted">{hint}</div>}
    </div>
  );
}
