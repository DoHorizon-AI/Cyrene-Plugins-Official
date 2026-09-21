// -----------------------------------------------------------------------------
// Module: src/pages.tsx
// Role: The seven Navigator console pages and their owning Product reads.
// -----------------------------------------------------------------------------

import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import {
  count,
  Button,
  Field,
  formatDate,
  MetricCard,
  PageHeader,
  Panel,
  ResourceTable,
  StateBlock,
  StatusPill,
  text,
} from "./components";
import {
  type ApiKeyMetadata,
  type CredentialMetadata,
  type CreateModelImportInput,
  type JsonRecord,
  NAVIGATOR_PROXY_PATHS,
  NavigatorApi,
  NavigatorContractError,
  NavigatorHttpError,
  type SessionPayload,
  type SystemStatus,
} from "./api";

export interface PageProps {
  api: NavigatorApi;
}

interface SettingsPageProps extends PageProps {
  session: SessionPayload;
}

/** Workspace-level status cards and independently refreshed service observations. */
export function OverviewPage({ api }: PageProps) {
  const [reloadKey, setReloadKey] = useState(0);
  const [loading, setLoading] = useState(true);
  const [system, setSystem] = useState<SystemStatus | null>(null);
  const [models, setModels] = useState<JsonRecord[] | null>(null);
  const [datasets, setDatasets] = useState<JsonRecord[] | null>(null);
  const [drafts, setDrafts] = useState<JsonRecord[] | null>(null);
  const [deployments, setDeployments] = useState<JsonRecord[] | null>(null);
  const [failures, setFailures] = useState<string[]>([]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    void Promise.allSettled([
      api.getSystemStatus(),
      api.getModelImports(),
      api.getDatasets(),
      api.getTrainingDrafts(),
      api.getDeployments(),
    ]).then(([systemResult, modelsResult, datasetsResult, draftsResult, deploymentsResult]) => {
      if (!active) {
        return;
      }
      const nextFailures: string[] = [];
      const nextSystem = settledValue(systemResult, "Host status", nextFailures);
      const nextModels = settledValue(modelsResult, "Models", nextFailures);
      const nextDatasets = settledValue(datasetsResult, "Datasets", nextFailures);
      const nextDrafts = settledValue(draftsResult, "Training", nextFailures);
      const nextDeployments = settledValue(deploymentsResult, "Deployments", nextFailures);
      setSystem(nextSystem);
      setModels(nextModels);
      setDatasets(nextDatasets);
      setDrafts(nextDrafts);
      setDeployments(nextDeployments);
      setFailures(nextFailures);
      setLoading(false);
    });
    return () => {
      active = false;
    };
  }, [api, reloadKey]);

  const refresh = () => setReloadKey((value) => value + 1);

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Workspace observatory / 00"
        title="The state of the stack."
        description="Navigator reads each owning Product directly. This surface is a pulse, not a second source of truth."
        action={
          <Button onClick={refresh} disabled={loading} aria-label="Refresh overview">
            {loading ? "Reading..." : "Refresh pulse"}
          </Button>
        }
      />

      <div className="metric-grid">
        <MetricCard
          label="Model imports"
          value={loading ? "..." : count(models?.length ?? null)}
          detail={models === null ? "Reactor unavailable" : "Reactor-owned"}
          accent="lime"
        />
        <MetricCard
          label="Dataset containers"
          value={loading ? "..." : count(datasets?.length ?? null)}
          detail={datasets === null ? "Catalyst unavailable" : "Catalyst-owned"}
          accent="blue"
        />
        <MetricCard
          label="Training drafts"
          value={loading ? "..." : count(drafts?.length ?? null)}
          detail={drafts === null ? "Yield unavailable" : "Yield-owned"}
          accent="orange"
        />
        <MetricCard
          label="Deployments"
          value={loading ? "..." : count(deployments?.length ?? null)}
          detail={deployments === null ? "Reactor unavailable" : "Reactor-owned"}
          accent="gray"
        />
      </div>

      {loading ? (
        <StateBlock kind="loading" title="Reading Product surfaces" detail="Navigator is asking each configured owner for a fresh projection." />
      ) : (
        <div className="overview-grid">
          <Panel
            title="Service reachability"
            meta={<StatusPill value={system?.status ?? "UNKNOWN"} />}
          >
            <div className="service-list">
              {[
                ["Navigator Web Host", system !== null],
                ["Reactor / models + serving", models !== null && deployments !== null],
                ["Catalyst / datasets", datasets !== null],
                ["Yield / training", drafts !== null],
              ].map(([label, available]) => (
                <div className="service-row" key={String(label)}>
                  <span className={`service-dot ${available ? "service-dot--good" : "service-dot--bad"}`} aria-hidden="true" />
                  <span>{label}</span>
                  <strong>{available ? "Available" : "Needs attention"}</strong>
                </div>
              ))}
            </div>
            <div className="panel-footnote">
              {system
                ? `${system.proxyPrefixes.length} proxy paths configured | observed ${formatDate(system.observedAt)}`
                : "The Web Host status endpoint did not return a projection."}
            </div>
          </Panel>

          <Panel title="Attention queue" meta={<span className="mono-label">LIVE READS</span>}>
            {failures.length === 0 ? (
              <StateBlock
                kind="empty"
                title="No blocked reads"
                detail="Every configured overview read answered. Resource state still belongs to its owning Product."
              />
            ) : (
              <div className="attention-list">
                {failures.map((failure) => (
                  <div className="attention-item" key={failure}>
                    <span className="attention-item__icon" aria-hidden="true">!</span>
                    <div>
                      <strong>{failure}</strong>
                      <p>Open the owning page and retry after checking its Product binding.</p>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Panel>

          <Panel
            title="GPU & Accelerators"
            meta={<StatusPill value={system?.gpu?.available ? "AVAILABLE" : "UNAVAILABLE"} />}
          >
            {system?.gpu?.available && system.gpu.gpus && system.gpu.gpus.length > 0 ? (
              <div className="service-list">
                {system.gpu.gpus.map((gpu, idx) => (
                  <div className="service-row" key={idx}>
                    <span className="service-dot service-dot--good" aria-hidden="true" />
                    <span><strong>{gpu.name}</strong></span>
                    <span>{gpu.usedMib} / {gpu.totalMib} MiB ({gpu.utilizationPct}% util)</span>
                  </div>
                ))}
              </div>
            ) : (
              <StateBlock
                kind="empty"
                title="No GPU detected"
                detail={system?.gpu?.available === false ? "nvidia-smi unavailable or no supported GPU found." : "Hardware information not reported."}
              />
            )}
          </Panel>

          <Panel
            title="Storage & Disk"
            meta={<StatusPill value={system?.disk?.available !== false ? "MOUNTED" : "UNAVAILABLE"} />}
          >
            {system?.disk?.totalGib ? (
              <dl className="detail-grid">
                <Detail label="Total space" value={`${system.disk.totalGib} GiB`} />
                <Detail label="Used space" value={`${system.disk.usedGib ?? "-"} GiB`} />
                <Detail label="Free space" value={`${system.disk.freeGib ?? "-"} GiB`} />
                <Detail label="Utilization" value={`${system.disk.usedPct ?? "-"}%`} />
              </dl>
            ) : (
              <StateBlock kind="empty" title="Storage usage unavailable" detail="Filesystem statistics not reported by host." />
            )}
          </Panel>
        </div>
      )}
    </div>
  );
}

/** Reactor model imports, binding choices, validation evidence, and import form. */
export function ModelsPage({ api }: PageProps) {
  const [reloadKey, setReloadKey] = useState(0);
  const [models, setModels] = useState<JsonRecord[] | null>(null);
  const [bindings, setBindings] = useState<JsonRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [bindingError, setBindingError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [sourceKind, setSourceKind] = useState<CreateModelImportInput["source"]["kind"]>("HUGGING_FACE");
  const [repository, setRepository] = useState("");
  const [revision, setRevision] = useState("");
  const [localPath, setLocalPath] = useState("");
  const [servingBindingId, setServingBindingId] = useState("");
  const [credentialRef, setCredentialRef] = useState("");

  useEffect(() => {
    let active = true;
    setLoading(true);
    void Promise.allSettled([api.getModelImports(), api.getServingBindings()]).then(([modelResult, bindingResult]) => {
      if (!active) {
        return;
      }
      if (modelResult.status === "fulfilled") {
        setModels(modelResult.value);
        setError(null);
      } else {
        setModels(null);
        setError(errorMessage(modelResult.reason));
      }
      if (bindingResult.status === "fulfilled") {
        setBindings(bindingResult.value);
        setBindingError(null);
      } else {
        setBindings([]);
        setBindingError(errorMessage(bindingResult.reason));
      }
      setLoading(false);
    });
    return () => {
      active = false;
    };
  }, [api, reloadKey]);

  const submitImport = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFormError(null);
    setNotice(null);
    const cleanName = name.trim();
    const cleanBinding = servingBindingId.trim();
    if (!cleanName || !cleanBinding) {
      setFormError("A model name and serving binding are required.");
      return;
    }
    const source =
      sourceKind === "HUGGING_FACE"
        ? { kind: "HUGGING_FACE" as const, repository: repository.trim(), revision: revision.trim() }
        : { kind: "LOCAL_PATH" as const, path: localPath.trim() };
    if (sourceKind === "HUGGING_FACE" && (!source.repository || !/^[a-f0-9]{40}$/i.test(source.revision))) {
      setFormError("Hugging Face imports require owner/name and a pinned 40-character revision.");
      return;
    }
    if (sourceKind === "LOCAL_PATH" && !localPath.trim().startsWith("/")) {
      setFormError("Local imports require an absolute path admitted by Reactor.");
      return;
    }
    const input: CreateModelImportInput = {
      name: cleanName,
      servingBindingId: cleanBinding,
      source,
      trustRemoteCode: false,
      ...(credentialRef.trim() ? { credentialRef: credentialRef.trim() } : {}),
    };
    setSubmitting(true);
    try {
      await api.createModelImport(input);
      setName("");
      setRepository("");
      setRevision("");
      setLocalPath("");
      setCredentialRef("");
      setNotice("Model import submitted. Reactor owns validation progress.");
      setReloadKey((value) => value + 1);
    } catch (submitError) {
      setFormError(errorMessage(submitError));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Reactor / 01"
        title="Models with receipts."
        description="Import a pinned source, then read Reactor's validation evidence before handing the artifact onward."
        action={<Button onClick={() => setReloadKey((value) => value + 1)} disabled={loading}>Refresh models</Button>}
      />

      <Panel title="Import a model" meta={<span className="mono-label">TRUST REMOTE CODE: OFF</span>}>
        <form className="form-grid" onSubmit={submitImport}>
          <Field label="Display name">
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Qwen 2.5 1.5B Instruct" />
          </Field>
          <Field label="Serving binding" hint={bindingError ?? "The binding is selected from Reactor's live list."}>
            <input
              value={servingBindingId}
              onChange={(event) => setServingBindingId(event.target.value)}
              list="serving-bindings"
              placeholder="llama-factory-serving-v1"
            />
            <datalist id="serving-bindings">
              {bindings.map((binding, index) => (
                <option key={resourceId(binding, index)} value={text(binding["bindingId"] ?? binding["id"])} />
              ))}
            </datalist>
          </Field>
          <Field label="Source kind">
            <select value={sourceKind} onChange={(event) => setSourceKind(event.target.value as CreateModelImportInput["source"]["kind"])}>
              <option value="HUGGING_FACE">Hugging Face repository</option>
              <option value="LOCAL_PATH">Local absolute path</option>
            </select>
          </Field>
          {sourceKind === "HUGGING_FACE" ? (
            <>
              <Field label="Repository" hint="owner/name">
                <input value={repository} onChange={(event) => setRepository(event.target.value)} placeholder="Qwen/Qwen2.5-1.5B-Instruct" />
              </Field>
              <Field label="Pinned revision" hint="40 hexadecimal characters">
                <input className="input-mono" value={revision} onChange={(event) => setRevision(event.target.value)} placeholder="5fee7c4e..." />
              </Field>
            </>
          ) : (
            <Field label="Absolute path" hint="Reactor validates access on the configured host.">
              <input className="input-mono" value={localPath} onChange={(event) => setLocalPath(event.target.value)} placeholder="/models/weights" />
            </Field>
          )}
          <Field label="Credential reference" hint="Optional write-only Web Host credential reference.">
            <input className="input-mono" value={credentialRef} onChange={(event) => setCredentialRef(event.target.value)} placeholder="credential://..." />
          </Field>
          <div className="form-actions">
            <Button tone="primary" type="submit" disabled={submitting}>{submitting ? "Submitting..." : "Start import"}</Button>
            {formError ? <span className="form-message form-message--error" role="alert">{formError}</span> : null}
            {notice ? <span className="form-message form-message--success">{notice}</span> : null}
          </div>
        </form>
      </Panel>

      <Panel title="Import ledger" meta={models ? `${models.length} records` : "LIVE READ"}>
        {loading ? (
          <StateBlock kind="loading" title="Reading model imports" detail="Reactor is the authority for import state and validation evidence." />
        ) : error ? (
          <StateBlock kind="error" title="Model imports unavailable" detail={error} action={<Button onClick={() => setReloadKey((value) => value + 1)}>Try again</Button>} />
        ) : models && models.length > 0 ? (
          <ResourceTable
            rows={models}
            rowKey={resourceId}
            caption="Reactor model imports"
            columns={[
              { label: "Model", render: (row) => <strong>{text(row["name"], "Unnamed import")}</strong> },
              { label: "State", render: (row) => <StatusPill value={text(row["state"], "UNKNOWN")} /> },
              { label: "Binding", render: (row) => <span className="input-mono">{text(row["servingBindingId"])}</span> },
              { label: "Evidence", render: (row) => validationSummary(row) },
              { label: "Updated", render: (row) => formatDate(row["updatedAt"]) },
            ]}
          />
        ) : (
          <StateBlock kind="empty" title="No imports yet" detail="Submit a pinned model source above. Reactor will publish validation evidence here." />
        )}
      </Panel>
    </div>
  );
}

/** Catalyst dataset containers with a deliberately small create surface. */
export function DatasetsPage({ api }: PageProps) {
  const [reloadKey, setReloadKey] = useState(0);
  const [datasets, setDatasets] = useState<JsonRecord[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  useEffect(() => {
    let active = true;
    setLoading(true);
    void api.getDatasets().then(
      (value) => {
        if (active) {
          setDatasets(value);
          setError(null);
          setLoading(false);
        }
      },
      (reason: unknown) => {
        if (active) {
          setDatasets(null);
          setError(errorMessage(reason));
          setLoading(false);
        }
      },
    );
    return () => {
      active = false;
    };
  }, [api, reloadKey]);

  const submitDataset = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const cleanName = name.trim();
    if (!cleanName) {
      setFormError("A dataset name is required.");
      return;
    }
    setSubmitting(true);
    setFormError(null);
    setNotice(null);
    try {
      await api.createDataset({ name: cleanName, description: description.trim() });
      setName("");
      setDescription("");
      setNotice("Dataset container created. Upload and preparation remain Catalyst-owned steps.");
      setReloadKey((value) => value + 1);
    } catch (submitError) {
      setFormError(errorMessage(submitError));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Catalyst / 02"
        title="Make the data legible."
        description="Create the owned container first. Preparation, mapping, quality, and split decisions stay visible at Catalyst rather than being guessed here."
        action={<Button onClick={() => setReloadKey((value) => value + 1)} disabled={loading}>Refresh datasets</Button>}
      />

      <Panel title="New dataset container" meta={<span className="mono-label">CATALYST OWNS STATE</span>}>
        <form className="form-grid form-grid--compact" onSubmit={submitDataset}>
          <Field label="Name">
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="instruction-tuning-v1" />
          </Field>
          <Field label="Description" hint="Optional context for the preparation team.">
            <input value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Curated instruction examples" />
          </Field>
          <div className="form-actions">
            <Button tone="primary" type="submit" disabled={submitting}>{submitting ? "Creating..." : "Create dataset"}</Button>
            {formError ? <span className="form-message form-message--error" role="alert">{formError}</span> : null}
            {notice ? <span className="form-message form-message--success">{notice}</span> : null}
          </div>
        </form>
      </Panel>

      <Panel title="Dataset containers" meta={datasets ? `${datasets.length} records` : "LIVE READ"}>
        {loading ? (
          <StateBlock kind="loading" title="Reading datasets" detail="Catalyst is the authority for containers and their lifecycle." />
        ) : error ? (
          <StateBlock kind="error" title="Datasets unavailable" detail={error} action={<Button onClick={() => setReloadKey((value) => value + 1)}>Try again</Button>} />
        ) : datasets && datasets.length > 0 ? (
          <ResourceTable
            rows={datasets}
            rowKey={resourceId}
            caption="Catalyst datasets"
            columns={[
              { label: "Dataset", render: (row) => <strong>{text(row["name"], "Unnamed dataset")}</strong> },
              { label: "State", render: (row) => <StatusPill value={text(row["state"], "UNKNOWN")} /> },
              { label: "Description", render: (row) => text(row["description"], "No description") },
              { label: "Version", render: (row) => <span className="input-mono">v{text(row["resourceVersion"], "-")}</span> },
              { label: "Updated", render: (row) => formatDate(row["updatedAt"]) },
            ]}
          />
        ) : (
          <StateBlock kind="empty" title="No dataset containers" detail="Create the container that will own your next preparation flow." />
        )}
      </Panel>
    </div>
  );
}

/** Training draft list with explicit launch actions and no fabricated run list. */
export function TrainingPage({ api }: PageProps) {
  const [reloadKey, setReloadKey] = useState(0);
  const [drafts, setDrafts] = useState<JsonRecord[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionId, setActionId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    void api.getTrainingDrafts().then(
      (value) => {
        if (active) {
          setDrafts(value);
          setError(null);
          setLoading(false);
        }
      },
      (reason: unknown) => {
        if (active) {
          setDrafts(null);
          setError(errorMessage(reason));
          setLoading(false);
        }
      },
    );
    return () => {
      active = false;
    };
  }, [api, reloadKey]);

  const startDraft = async (id: string) => {
    setActionId(id);
    setActionError(null);
    try {
      await api.startTrainingDraft(id);
      setReloadKey((value) => value + 1);
    } catch (startError) {
      setActionError(errorMessage(startError));
    } finally {
      setActionId(null);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Yield / 03"
        title="Train only from prepared intent."
        description="Training drafts are owned by Yield. This console can launch a prepared draft and then hands run observation to the Runs page."
        action={<Button onClick={() => setReloadKey((value) => value + 1)} disabled={loading}>Refresh drafts</Button>}
      />

      <div className="callout callout--orange">
        <span className="callout__mark" aria-hidden="true">i</span>
        <p><strong>Preflight belongs to the owner.</strong> The UI never infers GPU readiness from browser state; Yield returns the authoritative preflight and launch result.</p>
      </div>

      <Panel title="Training drafts" meta={drafts ? `${drafts.length} records` : "LIVE READ"}>
        {loading ? (
          <StateBlock kind="loading" title="Reading training drafts" detail="Yield is the authority for draft readiness and launch intent." />
        ) : error ? (
          <StateBlock kind="error" title="Training drafts unavailable" detail={error} action={<Button onClick={() => setReloadKey((value) => value + 1)}>Try again</Button>} />
        ) : drafts && drafts.length > 0 ? (
          <>
            <ResourceTable
              rows={drafts}
              rowKey={resourceId}
              caption="Yield training drafts"
              columns={[
                { label: "Draft", render: (row) => <strong>{text(row["name"], "Unnamed draft")}</strong> },
                { label: "State", render: (row) => <StatusPill value={text(row["state"], "UNKNOWN")} /> },
                { label: "Dataset version", render: (row) => datasetVersionLabel(row) },
                { label: "Workspace", render: (row) => text(row["workspaceId"], "default") },
                { label: "Created", render: (row) => formatDate(row["createdAt"]) },
                {
                  label: "Action",
                  render: (row) => {
                    const id = text(row["id"], "");
                    const prepared = text(row["state"], "") === "PREPARED";
                    return (
                      <Button
                        tone="primary"
                        disabled={!prepared || actionId !== null}
                        onClick={() => void startDraft(id)}
                        title={prepared ? "Start this prepared draft" : "Prepare this draft in Yield first"}
                      >
                        {actionId === id ? "Launching..." : prepared ? "Start run" : "Not ready"}
                      </Button>
                    );
                  },
                },
              ]}
            />
            {actionError ? <p className="inline-error" role="alert">{actionError}</p> : null}
          </>
        ) : (
          <StateBlock kind="empty" title="No training drafts" detail="Publish a DatasetVersion and create a draft in Yield before launching training." />
        )}
      </Panel>
    </div>
  );
}

/** Run lookup and attempt diagnostics, reflecting the published Yield API shape. */
export function RunsPage({ api }: PageProps) {
  const [runId, setRunId] = useState(() => initialRunId());
  const [run, setRun] = useState<JsonRecord | null>(null);
  const [attempts, setAttempts] = useState<JsonRecord[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attemptError, setAttemptError] = useState<string | null>(null);
  const [canceling, setCanceling] = useState(false);

  // SSE Realtime events stream state
  const [events, setEvents] = useState<JsonRecord[]>([]);
  const [latestLoss, setLatestLoss] = useState<number | null>(null);
  const [currentStep, setCurrentStep] = useState<number | null>(null);
  const [totalSteps, setTotalSteps] = useState<number | null>(null);
  const [streamActive, setStreamActive] = useState(false);
  const [streamDone, setStreamDone] = useState(false);

  useEffect(() => {
    const value = initialRunId();
    if (!value) {
      return;
    }
    let active = true;
    setLoading(true);
    void api.getTrainingRun(value).then(
      (result) => {
        if (!active) {
          return;
        }
        setRun(result);
        setError(null);
        setLoading(false);
        void api.getTrainingRunAttempts(value).then(
          (attemptResult) => active && setAttempts(attemptResult),
          (attemptReason: unknown) => active && setAttemptError(errorMessage(attemptReason)),
        );
      },
      (reason: unknown) => {
        if (active) {
          setRun(null);
          setError(errorMessage(reason));
          setLoading(false);
        }
      },
    );
    return () => {
      active = false;
    };
  }, [api]);

  const currentRunId = run ? text(run["id"], "") : "";
  const runState = text(run?.["state"], "");
  const canCancel = ["QUEUED", "RUNNING", "AWAITING_RETRY"].includes(runState);

  useEffect(() => {
    if (!currentRunId || !["RUNNING", "QUEUED"].includes(runState)) {
      return;
    }

    let canceled = false;
    let retryCount = 0;
    let controller = new AbortController();

    const connect = async () => {
      if (canceled) return;
      controller = new AbortController();
      const url = `${NAVIGATOR_PROXY_PATHS.yield}/api/v1/training-runs/${currentRunId}/events/stream`;
      try {
        setStreamActive(true);
        const response = await fetch(url, {
          signal: controller.signal,
          headers: { Accept: "text/event-stream" },
          credentials: "same-origin",
        });
        if (!response.ok || !response.body) {
          throw new Error(`HTTP ${response.status}`);
        }
        retryCount = 0;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (!canceled) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? "";

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed) continue;
            if (trimmed.startsWith("event:")) {
              const eventKind = trimmed.substring(6).trim();
              if (eventKind === "done") {
                setStreamDone(true);
                setStreamActive(false);
                void api.getTrainingRun(currentRunId).then((updated) => setRun(updated));
                return;
              }
            } else if (trimmed.startsWith("data:")) {
              const jsonStr = trimmed.substring(5).trim();
              try {
                const parsed = JSON.parse(jsonStr) as Record<string, unknown>;
                setEvents((prev) => [...prev.slice(-49), parsed]);
                const payload = (parsed["payload"] ?? parsed) as Record<string, unknown>;
                if (typeof payload["loss"] === "number") {
                  setLatestLoss(payload["loss"]);
                }
                const step = payload["step"] ?? payload["currentStep"] ?? payload["current_step"];
                if (typeof step === "number") {
                  setCurrentStep(Number(step));
                }
                const total = payload["totalSteps"] ?? payload["total_steps"] ?? payload["total"];
                if (typeof total === "number") {
                  setTotalSteps(Number(total));
                }
              } catch {
                // Ignore parse errors
              }
            }
          }
        }
      } catch {
        if (canceled) return;
        setStreamActive(false);
        if (retryCount < 3) {
          retryCount += 1;
          setTimeout(() => {
            if (!canceled) {
              void connect();
            }
          }, 5000);
        }
      }
    };

    void connect();

    return () => {
      canceled = true;
      controller.abort();
    };
  }, [currentRunId, runState, api]);

  const lookupRun = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const value = runId.trim();
    if (!value) {
      setError("Enter a training run id to read its owning Product projection.");
      return;
    }
    updateRunQuery(value);
    setLoading(true);
    setError(null);
    setAttemptError(null);
    setRun(null);
    setAttempts(null);
    setEvents([]);
    setLatestLoss(null);
    setCurrentStep(null);
    setTotalSteps(null);
    setStreamDone(false);
    try {
      const result = await api.getTrainingRun(value);
      setRun(result);
      try {
        setAttempts(await api.getTrainingRunAttempts(value));
      } catch (attemptReason) {
        setAttemptError(errorMessage(attemptReason));
      }
    } catch (lookupError) {
      setError(errorMessage(lookupError));
    } finally {
      setLoading(false);
    }
  };

  const cancelRun = async () => {
    const id = run ? text(run["id"], "") : "";
    if (!id) {
      return;
    }
    setCanceling(true);
    setError(null);
    try {
      setRun(await api.cancelTrainingRun(id));
    } catch (cancelError) {
      setError(errorMessage(cancelError));
    } finally {
      setCanceling(false);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Yield / 04"
        title="Follow one run to the metal."
        description="Yield publishes run detail and attempt diagnostics, not a browser-owned history cache. Enter an id to read the current projection."
      />

      <Panel title="Find a training run" meta={<span className="mono-label">OWNER: YIELD</span>}>
        <form className="lookup-form" onSubmit={lookupRun}>
          <Field label="Training run id" hint="Use the id returned when a prepared draft starts.">
            <input className="input-mono" value={runId} onChange={(event) => setRunId(event.target.value)} placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" />
          </Field>
          <Button tone="primary" type="submit" disabled={loading}>{loading ? "Reading..." : "Read run"}</Button>
        </form>
      </Panel>

      {loading ? <StateBlock kind="loading" title="Reading run projection" detail="Yield is returning current run state and public attempt diagnostics." /> : null}
      {error ? <StateBlock kind="error" title="Run could not be read" detail={error} /> : null}
      {run ? (
        <>
          <Panel
            title={text(run["name"], "Training run")}
            meta={
              <div className="panel-actions">
                <StatusPill value={runState || "UNKNOWN"} />
                {canCancel ? <Button tone="danger" onClick={() => void cancelRun()} disabled={canceling}>{canceling ? "Canceling..." : "Request cancel"}</Button> : null}
              </div>
            }
          >
            <dl className="detail-grid">
              <Detail label="Run id" value={text(run["id"])} mono />
              <Detail label="Engine binding" value={text(run["engineBindingId"])} mono />
              <Detail label="Attempts" value={text(run["attemptCount"], "0")} />
              <Detail label="Resource version" value={text(run["resourceVersion"], "-")} />
              <Detail label="Created" value={formatDate(run["createdAt"])} />
              <Detail label="Updated" value={formatDate(run["updatedAt"])} />
            </dl>
            {nestedRecord(run, "failure") ? (
              <div className="callout callout--red">
                <span className="callout__mark" aria-hidden="true">!</span>
                <p><strong>{text(nestedRecord(run, "failure")?.["code"], "Run failure")}</strong> {text(nestedRecord(run, "failure")?.["message"])}</p>
              </div>
            ) : null}
          </Panel>

          <Panel
            title="Realtime execution stream"
            meta={
              <div className="panel-actions">
                <StatusPill value={streamActive ? "STREAMING" : streamDone || ["SUCCEEDED", "FAILED", "CANCELLED"].includes(runState) ? "FINISHED" : "IDLE"} />
                {(streamDone || ["SUCCEEDED", "FAILED", "CANCELLED"].includes(runState)) && (
                  <Button onClick={() => void api.getTrainingRun(currentRunId).then((res) => setRun(res))}>
                    查看结果
                  </Button>
                )}
              </div>
            }
          >
            <div className="metric-grid">
              <MetricCard
                label="Loss"
                value={latestLoss !== null ? latestLoss.toFixed(4) : "--"}
                detail="Current training loss"
                accent="orange"
              />
              <MetricCard
                label="Progress"
                value={currentStep !== null && totalSteps !== null ? `${currentStep} / ${totalSteps}` : currentStep !== null ? `Step ${currentStep}` : "--"}
                detail={currentStep !== null && totalSteps ? `${Math.round((currentStep / totalSteps) * 100)}% steps completed` : "Step progress"}
                accent="blue"
              />
              <MetricCard
                label="Stream status"
                value={streamActive ? "Active" : streamDone ? "Finished" : "Standby"}
                detail={streamActive ? "SSE live connection" : "Stream completed or disconnected"}
                accent={streamActive ? "lime" : "gray"}
              />
            </div>

            <div style={{ marginTop: "16px" }}>
              <strong style={{ display: "block", marginBottom: "8px", fontSize: "13px" }}>Event logs (last 50):</strong>
              <div style={{ maxHeight: "260px", overflowY: "auto", background: "var(--color-bg-subtle, #181c20)", padding: "12px", borderRadius: "6px", fontFamily: "monospace", fontSize: "12px" }}>
                {events.length === 0 ? (
                  <div style={{ color: "var(--muted, #888)" }}>No realtime stream events captured yet.</div>
                ) : (
                  events.map((evt, idx) => {
                    const seq = String(evt["sequence"] ?? idx + 1);
                    const kind = String(evt["kind"] ?? evt["event"] ?? "event");
                    const payload = evt["payload"] ? JSON.stringify(evt["payload"]) : evt["message"] ?? JSON.stringify(evt);
                    return (
                      <div key={idx} style={{ marginBottom: "4px", lineHeight: "1.4" }}>
                        <span style={{ color: "var(--muted, #888)", marginRight: "8px" }}>#{seq}</span>
                        <span style={{ color: "var(--blue, #64B5F6)", marginRight: "8px" }}>[{kind}]</span>
                        <span>{String(payload)}</span>
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          </Panel>

          <Panel title="Attempt diagnostics" meta={attempts ? `${attempts.length} attempts` : "OWNER READ"}>
            {attemptError ? (
              <StateBlock kind="error" title="Attempts unavailable" detail={attemptError} />
            ) : attempts && attempts.length > 0 ? (
              <ResourceTable
                rows={attempts}
                rowKey={resourceId}
                caption="Yield training attempts"
                columns={[
                  { label: "Attempt", render: (row) => <span className="input-mono">{text(row["id"])}</span> },
                  { label: "Phase", render: (row) => text(row["phase"]) },
                  { label: "State", render: (row) => <StatusPill value={text(row["state"], "UNKNOWN")} /> },
                  { label: "Failure", render: (row) => text(nestedRecord(row, "failure")?.["message"], "No failure recorded") },
                ]}
              />
            ) : (
              <StateBlock kind="empty" title="No attempt diagnostics" detail="Yield has not published attempt records for this run." />
            )}
          </Panel>
        </>
      ) : !loading && !error ? (
        <StateBlock kind="empty" title="No run selected" detail="Run history stays with Yield. Enter a run id above to inspect a live projection." />
      ) : null}
    </div>
  );
}

/** Reactor deployment intent, observed state, and explicit stop action. */
export function DeploymentsPage({ api }: PageProps) {
  const [reloadKey, setReloadKey] = useState(0);
  const [deployments, setDeployments] = useState<JsonRecord[] | null>(null);
  const [bindings, setBindings] = useState<JsonRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [bindingError, setBindingError] = useState<string | null>(null);
  const [stopId, setStopId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    void Promise.allSettled([api.getDeployments(), api.getServingBindings()]).then(([deploymentResult, bindingResult]) => {
      if (!active) {
        return;
      }
      if (deploymentResult.status === "fulfilled") {
        setDeployments(deploymentResult.value);
        setError(null);
      } else {
        setDeployments(null);
        setError(errorMessage(deploymentResult.reason));
      }
      if (bindingResult.status === "fulfilled") {
        setBindings(bindingResult.value);
        setBindingError(null);
      } else {
        setBindings([]);
        setBindingError(errorMessage(bindingResult.reason));
      }
      setLoading(false);
    });
    return () => {
      active = false;
    };
  }, [api, reloadKey]);

  const stopDeployment = async (id: string) => {
    setStopId(id);
    setActionError(null);
    try {
      await api.stopDeployment(id);
      setReloadKey((value) => value + 1);
    } catch (stopError) {
      setActionError(errorMessage(stopError));
    } finally {
      setStopId(null);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Reactor / 05"
        title="Serve with a known edge."
        description="Deployments express intent; endpoints express addressability. Read both through Reactor and stop only through its explicit lifecycle action."
        action={<Button onClick={() => setReloadKey((value) => value + 1)} disabled={loading}>Refresh deployments</Button>}
      />

      <div className="callout callout--blue">
        <span className="callout__mark" aria-hidden="true">i</span>
        <p><strong>Serving bindings: {bindings.length || "none reported"}.</strong> {bindingError ?? "Reactor resolves node and engine identity; the UI does not infer readiness from a URL."}</p>
      </div>

      <Panel title="Deployment ledger" meta={deployments ? `${deployments.length} records` : "LIVE READ"}>
        {loading ? (
          <StateBlock kind="loading" title="Reading deployments" detail="Reactor is reconciling desired intent with observed serving state." />
        ) : error ? (
          <StateBlock kind="error" title="Deployments unavailable" detail={error} action={<Button onClick={() => setReloadKey((value) => value + 1)}>Try again</Button>} />
        ) : deployments && deployments.length > 0 ? (
          <>
            <ResourceTable
              rows={deployments}
              rowKey={resourceId}
              caption="Reactor deployments"
              columns={[
                { label: "Deployment", render: (row) => <strong>{text(row["name"], "Unnamed deployment")}</strong> },
                { label: "Observed", render: (row) => <StatusPill value={text(row["observedState"], "UNKNOWN")} /> },
                { label: "Desired", render: (row) => <StatusPill value={text(row["desiredState"], "UNKNOWN")} /> },
                { label: "Binding", render: (row) => <span className="input-mono">{text(row["servingBindingId"])}</span> },
                { label: "Endpoint", render: (row) => endpointSummary(row) },
                {
                  label: "Action",
                  render: (row) => {
                    const id = text(row["id"], "");
                    const stopped = text(row["desiredState"], "") === "STOPPED";
                    return <Button tone="danger" disabled={stopped || stopId !== null} onClick={() => void stopDeployment(id)}>{stopId === id ? "Stopping..." : stopped ? "Stopped" : "Stop"}</Button>;
                  },
                },
              ]}
            />
            {actionError ? <p className="inline-error" role="alert">{actionError}</p> : null}
          </>
        ) : (
          <StateBlock kind="empty" title="No deployments" detail="A validated model version must be handed to Reactor before a serving deployment can exist." />
        )}
      </Panel>
    </div>
  );
}

/** Exchange Gateway routes, invocation snippets, and API key lifecycle administration. */
export function GatewayPage({ api }: PageProps) {
  const [reloadKey, setReloadKey] = useState(0);
  const [routes, setRoutes] = useState<JsonRecord[] | null>(null);
  const [selectedRoute, setSelectedRoute] = useState<JsonRecord | null>(null);
  const [apiKeys, setApiKeys] = useState<ApiKeyMetadata[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [activeCodeTab, setActiveCodeTab] = useState<"curl" | "python" | "javascript">("curl");

  // New key form state
  const [keyName, setKeyName] = useState("");
  const [expiresDays, setExpiresDays] = useState("");
  const [modelScope, setModelScope] = useState("");
  const [creatingKey, setCreatingKey] = useState(false);
  const [createdSecret, setCreatedSecret] = useState<string | null>(null);
  const [createdKeyName, setCreatedKeyName] = useState<string | null>(null);
  const [copiedKey, setCopiedKey] = useState(false);
  const [copiedSnippet, setCopiedSnippet] = useState(false);
  const [copiedBaseUrl, setCopiedBaseUrl] = useState(false);

  useEffect(() => {
    let active = true;
    setLoading(true);
    void Promise.allSettled([api.getGatewayRoutes(), api.listApiKeys()]).then(
      ([routesRes, keysRes]) => {
        if (!active) return;
        if (routesRes.status === "fulfilled") {
          setRoutes(routesRes.value);
          if (routesRes.value.length > 0) {
            setSelectedRoute((prev) => prev ?? routesRes.value[0]);
          }
          setError(null);
        } else {
          setRoutes(null);
          setError(errorMessage(routesRes.reason));
        }

        if (keysRes.status === "fulfilled") {
          setApiKeys(keysRes.value);
        } else {
          setApiKeys([]);
        }
        setLoading(false);
      },
    );
    return () => {
      active = false;
    };
  }, [api, reloadKey]);

  const refresh = () => setReloadKey((v) => v + 1);

  const confirmRoute = async (route: JsonRecord) => {
    const id = text(route["id"]);
    const version = typeof route["resourceVersion"] === "number" ? route["resourceVersion"] : 1;
    setActionError(null);
    setActionNotice(null);
    try {
      await api.confirmGatewayRoute(id, version);
      setActionNotice(`Route ${text(route["modelPattern"] || route["model_pattern"] || id)} confirmed and published.`);
      refresh();
    } catch (err) {
      setActionError(errorMessage(err));
    }
  };

  const handleCreateKey = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const name = keyName.trim();
    if (!name) return;
    setCreatingKey(true);
    setActionError(null);
    try {
      let expiresAt: string | null = null;
      const days = parseInt(expiresDays, 10);
      if (!isNaN(days) && days > 0) {
        expiresAt = new Date(Date.now() + days * 86400000).toISOString();
      }
      const scope = modelScope
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const res = await api.createApiKey(text(selectedRoute?.["id"] ?? ""), {
        name,
        expiresAt,
        modelScope: scope,
      });
      setCreatedSecret(res.secret);
      setCreatedKeyName(res.key.name);
      setKeyName("");
      setExpiresDays("");
      setModelScope("");
      refresh();
    } catch (err) {
      setActionError(errorMessage(err));
    } finally {
      setCreatingKey(false);
    }
  };

  const handleRevokeKey = async (id: string, name: string) => {
    if (!window.confirm(`Are you sure you want to revoke API key "${name}"? This action is immediate and permanent.`)) {
      return;
    }
    setActionError(null);
    try {
      await api.revokeApiKey(id);
      refresh();
    } catch (err) {
      setActionError(errorMessage(err));
    }
  };

  const baseUrl = typeof window !== "undefined"
    ? `${window.location.protocol}//${window.location.hostname}:8003/v1`
    : "http://localhost:8003/v1";

  const modelId = text(
    selectedRoute?.["modelPattern"] ?? selectedRoute?.["model_pattern"] ?? selectedRoute?.["targetModel"] ?? "default-model"
  );

  const snippets = {
    curl: `curl ${baseUrl}/chat/completions \\
  -H "Authorization: Bearer <API_KEY>" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${modelId}", "messages": [{"role":"user","content":"Hello"}]}'`,
    python: `from openai import OpenAI

client = OpenAI(api_key="<API_KEY>", base_url="${baseUrl}")
response = client.chat.completions.create(
    model="${modelId}",
    messages=[{"role": "user", "content": "Hello"}]
)
print(response.choices[0].message.content)`,
    javascript: `import OpenAI from 'openai';

const client = new OpenAI({ apiKey: '<API_KEY>', baseURL: '${baseUrl}' });
const response = await client.chat.completions.create({
    model: '${modelId}',
    messages: [{ role: 'user', content: 'Hello' }],
});
console.log(response.choices[0].message.content);`,
  };

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Exchange / Gateway"
        title="Routes, API keys, and client configuration."
        description="Exchange acts as the single OpenAI-compatible data plane. Publish model routes, copy client integration code, and manage caller API keys."
        action={
          <Button onClick={refresh} disabled={loading} aria-label="Refresh gateway">
            {loading ? "Reading..." : "Refresh"}
          </Button>
        }
      />

      {actionNotice ? (
        <div className="callout callout--blue">
          <span className="callout__mark" aria-hidden="true">✓</span>
          <p>{actionNotice}</p>
        </div>
      ) : null}
      {actionError ? (
        <div className="callout callout--red">
          <span className="callout__mark" aria-hidden="true">!</span>
          <p><strong>Error:</strong> {actionError}</p>
        </div>
      ) : null}

      {/* 1. Gateway Routes Table */}
      <Panel
        title="Gateway routes"
        meta={routes ? `${routes.length} configured` : "EXCHANGE OWNER"}
      >
        {loading && !routes ? (
          <StateBlock kind="loading" title="Reading Gateway routes" detail="Exchange is returning published model routes." />
        ) : error ? (
          <StateBlock kind="error" title="Routes unavailable" detail={error} />
        ) : routes && routes.length > 0 ? (
          <ResourceTable
            rows={routes}
            rowKey={resourceId}
            caption="Exchange published routes"
            columns={[
              {
                label: "Model alias",
                render: (row) => {
                  const pattern = text(row["modelPattern"] || row["model_pattern"]);
                  const isSelected = selectedRoute && text(selectedRoute["id"]) === text(row["id"]);
                  return (
                    <button
                      className="link-button"
                      onClick={() => setSelectedRoute(row)}
                      style={{ fontWeight: isSelected ? "bold" : "normal", textDecoration: "underline", background: "none", border: "none", cursor: "pointer", color: "inherit", padding: 0 }}
                    >
                      {pattern} {isSelected ? "◀ (Selected)" : ""}
                    </button>
                  );
                },
              },
              {
                label: "Target binding",
                render: (row) => <span className="input-mono">{text(row["targetBindingId"] || row["target_binding_id"])}</span>,
              },
              {
                label: "Status",
                render: (row) => {
                  const state = text(row["state"], "ACTIVE");
                  return <StatusPill value={state} />;
                },
              },
              {
                label: "Action",
                render: (row) => {
                  const state = text(row["state"], "ACTIVE");
                  if (state === "DRAFT") {
                    return (
                      <Button tone="primary" onClick={() => void confirmRoute(row)}>
                        确认发布
                      </Button>
                    );
                  }
                  return (
                    <Button onClick={() => setSelectedRoute(row)}>
                      View detail
                    </Button>
                  );
                },
              },
            ]}
          />
        ) : (
          <StateBlock
            kind="empty"
            title="No Gateway routes"
            detail="Publish a serving deployment or add a route draft in Exchange to expose models."
          />
        )}
      </Panel>

      {/* 2. Selected Route Detail Panel */}
      {selectedRoute ? (
        <Panel
          title={`Route detail: ${modelId}`}
          meta={<StatusPill value={text(selectedRoute["state"], "ACTIVE")} />}
        >
          <dl className="detail-grid">
            <Detail label="Model ID" value={modelId} mono />
            <Detail
              label="Base URL"
              value={baseUrl}
              mono
            />
            <Detail label="Target binding" value={text(selectedRoute["targetBindingId"] || selectedRoute["target_binding_id"])} mono />
            <Detail label="Created" value={formatDate(selectedRoute["createdAt"] || selectedRoute["created_at"])} />
          </dl>

          <div style={{ marginTop: "12px", marginBottom: "16px" }}>
            <Button
              onClick={() => {
                void navigator.clipboard.writeText(baseUrl);
                setCopiedBaseUrl(true);
                setTimeout(() => setCopiedBaseUrl(false), 2000);
              }}
            >
              {copiedBaseUrl ? "Base URL Copied!" : "Copy Base URL"}
            </Button>
          </div>

          <div style={{ marginTop: "20px" }}>
            <div style={{ display: "flex", gap: "8px", marginBottom: "12px", alignItems: "center" }}>
              <strong style={{ marginRight: "12px" }}>Integration code:</strong>
              {(["curl", "python", "javascript"] as const).map((tab) => (
                <Button
                  key={tab}
                  tone={activeCodeTab === tab ? "primary" : "quiet"}
                  onClick={() => setActiveCodeTab(tab)}
                >
                  {tab === "curl" ? "cURL" : tab === "python" ? "Python" : "JavaScript"}
                </Button>
              ))}
              <Button
                onClick={() => {
                  void navigator.clipboard.writeText(snippets[activeCodeTab]);
                  setCopiedSnippet(true);
                  setTimeout(() => setCopiedSnippet(false), 2000);
                }}
              >
                {copiedSnippet ? "Copied!" : "Copy snippet"}
              </Button>
            </div>
            <pre style={{ background: "var(--color-bg-subtle, #181c20)", padding: "16px", borderRadius: "6px", overflowX: "auto", fontSize: "13px", lineHeight: "1.5" }}>
              <code>{snippets[activeCodeTab]}</code>
            </pre>
          </div>
        </Panel>
      ) : null}

      {/* 3. API Keys Management Panel */}
      <Panel
        title="Gateway API keys"
        meta={apiKeys ? `${apiKeys.filter((k) => k.state === "ACTIVE").length} active` : "EXCHANGE KEYS"}
      >
        {createdSecret ? (
          <div className="callout callout--orange" style={{ marginBottom: "20px", display: "block" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
              <span className="callout__mark" aria-hidden="true" style={{ fontSize: "18px", fontWeight: "bold" }}>⚠</span>
              <strong style={{ color: "var(--orange, #FF9800)" }}>API Key Created: {createdKeyName}</strong>
            </div>
            <p style={{ marginBottom: "12px" }}>
              此密钥不会再次显示，请立即复制并安全保存。关闭后将无法重新查看完整明文。
            </p>
            <div style={{ display: "flex", gap: "8px", alignItems: "center", marginBottom: "12px" }}>
              <input
                readOnly
                value={createdSecret}
                className="input-mono"
                style={{ flex: 1, padding: "8px 12px", fontSize: "14px", background: "rgba(0,0,0,0.3)" }}
              />
              <Button
                tone="primary"
                onClick={() => {
                  void navigator.clipboard.writeText(createdSecret);
                  setCopiedKey(true);
                  setTimeout(() => setCopiedKey(false), 2000);
                }}
              >
                {copiedKey ? "Copied!" : "Copy key"}
              </Button>
              <Button onClick={() => setCreatedSecret(null)}>
                Close
              </Button>
            </div>
          </div>
        ) : null}

        <form className="credential-form" onSubmit={handleCreateKey} style={{ marginBottom: "24px" }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: "12px", alignItems: "flex-end" }}>
            <Field label="Key name *" hint="Human-readable identifier">
              <input
                value={keyName}
                onChange={(e) => setKeyName(e.target.value)}
                placeholder="e.g. production-client"
                required
              />
            </Field>
            <Field label="Expiration (days)" hint="Optional (leave blank for no expiry)">
              <input
                type="number"
                min="1"
                value={expiresDays}
                onChange={(e) => setExpiresDays(e.target.value)}
                placeholder="e.g. 90"
              />
            </Field>
            <Field label="Model scope" hint="Optional comma-separated aliases">
              <input
                value={modelScope}
                onChange={(e) => setModelScope(e.target.value)}
                placeholder="default: all models"
              />
            </Field>
            <div>
              <Button tone="primary" type="submit" disabled={creatingKey || !keyName.trim()}>
                {creatingKey ? "Creating..." : "Create API Key"}
              </Button>
            </div>
          </div>
        </form>

        {apiKeys && apiKeys.length > 0 ? (
          <ResourceTable
            rows={apiKeys}
            rowKey={(row) => row.id}
            caption="Exchange API keys"
            columns={[
              { label: "Name", render: (row) => <strong>{row.name}</strong> },
              {
                label: "Key",
                render: (row) => <span className="input-mono">{`cyk_...${row.id.slice(-4)}`}</span>,
              },
              {
                label: "Status",
                render: (row) => <StatusPill value={row.state} />,
              },
              {
                label: "Model scope",
                render: (row) => row.modelScope && row.modelScope.length > 0 ? row.modelScope.join(", ") : "All models",
              },
              {
                label: "Created",
                render: (row) => formatDate(row.createdAt),
              },
              {
                label: "Expires",
                render: (row) => row.expiresAt ? formatDate(row.expiresAt) : "Never",
              },
              {
                label: "Action",
                render: (row) => {
                  if (row.state === "ACTIVE") {
                    return (
                      <Button tone="danger" onClick={() => void handleRevokeKey(row.id, row.name)}>
                        Revoke
                      </Button>
                    );
                  }
                  return <span style={{ color: "var(--muted)" }}>Revoked</span>;
                },
              },
            ]}
          />
        ) : (
          <StateBlock
            kind="empty"
            title="No API keys"
            detail="Create an API key above to allow client applications to authenticate with the Exchange gateway."
          />
        )}
      </Panel>
    </div>
  );
}

/** Web Host session metadata and write-only credential administration. */
export function SettingsPage({ api, session }: SettingsPageProps) {
  const [reloadKey, setReloadKey] = useState(0);
  const [system, setSystem] = useState<SystemStatus | null>(null);
  const [credentials, setCredentials] = useState<CredentialMetadata[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [systemError, setSystemError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [provider, setProvider] = useState("");
  const [kind, setKind] = useState("api-token");
  const [secret, setSecret] = useState("");

  useEffect(() => {
    let active = true;
    setLoading(true);
    void Promise.allSettled([api.getSystemStatus(), api.getCredentials()]).then(([systemResult, credentialsResult]) => {
      if (!active) {
        return;
      }
      if (systemResult.status === "fulfilled") {
        setSystem(systemResult.value);
        setSystemError(null);
      } else {
        setSystem(null);
        setSystemError(errorMessage(systemResult.reason));
      }
      if (credentialsResult.status === "fulfilled") {
        setCredentials(credentialsResult.value);
        setError(null);
      } else {
        setCredentials(null);
        setError(errorMessage(credentialsResult.reason));
      }
      setLoading(false);
    });
    return () => {
      active = false;
    };
  }, [api, reloadKey]);

  const createCredential = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!name.trim() || !provider.trim() || !secret) {
      setFormError("Name, provider, and secret are required.");
      return;
    }
    setSubmitting(true);
    setFormError(null);
    setNotice(null);
    try {
      await api.createCredential({ name: name.trim(), provider: provider.trim(), kind: kind.trim() || "generic", secret });
      setName("");
      setProvider("");
      setSecret("");
      setNotice("Credential metadata saved. The secret was accepted write-only and cannot be recovered here.");
      setReloadKey((value) => value + 1);
    } catch (createError) {
      setFormError(errorMessage(createError));
    } finally {
      setSubmitting(false);
    }
  };

  const revokeCredential = async (credential: CredentialMetadata) => {
    if (!window.confirm(`Revoke ${credential.name}? This cannot be undone.`)) {
      return;
    }
    setRevokingId(credential.id);
    setFormError(null);
    setNotice(null);
    try {
      await api.revokeCredential(credential.id);
      setNotice(`${credential.name} was revoked.`);
      setReloadKey((value) => value + 1);
    } catch (revokeError) {
      setFormError(errorMessage(revokeError));
    } finally {
      setRevokingId(null);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Web Host / 06"
        title="Keep the boundary boring."
        description="Session rotation, CSRF, proxy allowlists, and credential metadata are Web Host concerns. Secrets never become a Product read."
        action={<Button onClick={() => setReloadKey((value) => value + 1)} disabled={loading}>Refresh settings</Button>}
      />

      <div className="settings-grid">
        <Panel title="Current session" meta={<StatusPill value={session.state} />}>
          <dl className="detail-grid">
            <Detail label="Access state" value={session.authenticated ? "Authenticated" : "Anonymous"} />
            <Detail label="Access expires" value={formatDate(session.expiresAt)} />
            <Detail label="Refresh window" value={formatDate(session.refreshExpiresAt)} />
            <Detail label="Refreshable" value={session.refreshable ? "Yes" : "No"} />
          </dl>
          <p className="panel-footnote">Access and refresh cookies remain HttpOnly. The UI keeps only the rotating CSRF token in memory.</p>
        </Panel>

        <Panel title="Navigator Web Host" meta={<StatusPill value={system?.status ?? "UNKNOWN"} />}>
          {systemError ? <p className="inline-error">{systemError}</p> : null}
          <dl className="detail-grid">
            <Detail label="Service" value={system?.service ?? "Not reported"} mono />
            <Detail label="Version" value={system?.version ?? "Not reported"} mono />
            <Detail label="Active credentials" value={system ? String(system.credentials.active) : "--"} />
            <Detail label="Revoked credentials" value={system ? String(system.credentials.revoked) : "--"} />
          </dl>
          <div className="proxy-list">
            <p className="eyebrow">Configured proxy paths</p>
            {system?.proxyPrefixes.length ? system.proxyPrefixes.map((prefix) => <code key={prefix}>{prefix}</code>) : <span className="muted">No proxy prefixes reported.</span>}
          </div>
        </Panel>
      </div>

      <Panel title="Add credential metadata" meta={<span className="mono-label">SECRET NEVER READ BACK</span>}>
        <form className="form-grid" onSubmit={createCredential}>
          <Field label="Name">
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Hugging Face access" />
          </Field>
          <Field label="Provider">
            <input value={provider} onChange={(event) => setProvider(event.target.value)} placeholder="huggingface" />
          </Field>
          <Field label="Kind">
            <input value={kind} onChange={(event) => setKind(event.target.value)} placeholder="api-token" />
          </Field>
          <Field label="Secret" hint="The Web Host stores this for configured proxy resolution only.">
            <input type="password" value={secret} onChange={(event) => setSecret(event.target.value)} autoComplete="new-password" placeholder="Paste once, never displayed" />
          </Field>
          <div className="form-actions">
            <Button tone="primary" type="submit" disabled={submitting}>{submitting ? "Saving..." : "Save credential"}</Button>
            {formError ? <span className="form-message form-message--error" role="alert">{formError}</span> : null}
            {notice ? <span className="form-message form-message--success">{notice}</span> : null}
          </div>
        </form>
      </Panel>

      <Panel title="Credential metadata" meta={credentials ? `${credentials.length} records` : "LIVE READ"}>
        {loading ? (
          <StateBlock kind="loading" title="Reading credential metadata" detail="Only non-secret fields are returned by the Web Host." />
        ) : error ? (
          <StateBlock kind="error" title="Credentials unavailable" detail={error} action={<Button onClick={() => setReloadKey((value) => value + 1)}>Try again</Button>} />
        ) : credentials && credentials.length > 0 ? (
          <ResourceTable
            rows={credentials}
            rowKey={(row) => row.id}
            caption="Navigator Web Host credentials"
            columns={[
              { label: "Name", render: (row) => <strong>{row.name}</strong> },
              { label: "Provider", render: (row) => row.provider },
              { label: "Kind", render: (row) => <span className="input-mono">{row.kind}</span> },
              { label: "State", render: (row) => <StatusPill value={row.state} /> },
              { label: "Updated", render: (row) => formatDate(row.updatedAt) },
              { label: "Action", render: (row) => <Button tone="danger" disabled={row.state === "REVOKED" || revokingId !== null} onClick={() => void revokeCredential(row)}>{revokingId === row.id ? "Revoking..." : row.state === "REVOKED" ? "Revoked" : "Revoke"}</Button> },
            ]}
          />
        ) : (
          <StateBlock kind="empty" title="No credentials configured" detail="Add a write-only credential metadata record when a configured Product proxy needs it." />
        )}
      </Panel>
    </div>
  );
}

function Detail({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="detail-item">
      <dt>{label}</dt>
      <dd className={mono ? "input-mono" : undefined}>{value}</dd>
    </div>
  );
}

function settledValue<T>(result: PromiseSettledResult<T>, label: string, failures: string[]): T | null {
  if (result.status === "fulfilled") {
    return result.value;
  }
  failures.push(`${label}: ${errorMessage(result.reason)}`);
  return null;
}

function resourceId(row: JsonRecord, index = 0): string {
  return typeof row["id"] === "string" ? row["id"] : `resource-${index}`;
}

function nestedRecord(row: JsonRecord, key: string): JsonRecord | null {
  const value = row[key];
  return typeof value === "object" && value !== null && !Array.isArray(value) ? value as JsonRecord : null;
}

function datasetVersionLabel(row: JsonRecord): string {
  const version = nestedRecord(row, "datasetVersion");
  return text(version?.["id"], "Not attached");
}

function validationSummary(row: JsonRecord): string {
  const validation = nestedRecord(row, "validation");
  if (!validation) {
    return "Pending evidence";
  }
  const checks = ["weights", "config", "tokenizer", "chatTemplate"];
  const passed = checks.filter((key) => validation[key] === true).length;
  const issues = Array.isArray(validation["issues"]) ? validation["issues"].length : 0;
  return `${passed}/${checks.length} checks${issues ? ` | ${issues} issue${issues === 1 ? "" : "s"}` : ""}`;
}

function endpointSummary(row: JsonRecord) {
  const endpoint = row["endpointId"];
  return typeof endpoint === "string" ? <span className="input-mono">{endpoint}</span> : <span className="muted">Not ready</span>;
}

function initialRunId(): string {
  if (typeof window === "undefined") {
    return "";
  }
  return new URLSearchParams(window.location.search).get("runId") ?? "";
}

function updateRunQuery(runId: string): void {
  if (typeof window === "undefined") {
    return;
  }
  const url = new URL(window.location.href);
  url.searchParams.set("runId", runId);
  window.history.replaceState({}, "", url);
}

function errorMessage(error: unknown): string {
  if (error instanceof NavigatorHttpError) {
    return `${error.detail} (${error.code})`;
  }
  if (error instanceof NavigatorContractError) {
    return error.message;
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "The Navigator request failed without a readable error.";
}
