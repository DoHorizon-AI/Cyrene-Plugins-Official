// -----------------------------------------------------------------------------
// Module: src/pages.tsx
// Role: The seven Navigator console pages and their owning Product reads.
// -----------------------------------------------------------------------------
// 中文：// 中文：模块职责：实现 Navigator 控制台的七个页面及其所属 Product 的读取操作。

import { useEffect, useRef, useState } from "react";
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
  type ActiveRoutePayload,
  type ApiKeyMetadata,
  type CredentialMetadata,
  type CreateModelImportInput,
  type DatasetPreview,
  type DeploymentEvent,
  type JsonRecord,
  NAVIGATOR_PROXY_PATHS,
  NavigatorApi,
  NavigatorContractError,
  NavigatorHttpError,
  type SessionPayload,
  type SystemStatus,
  type TrainingParametersInput,
} from "./api";
import { pushRoute } from "./router";

export interface PageProps {
  api: NavigatorApi;
}

interface SettingsPageProps extends PageProps {
  session: SessionPayload;
}

/**
 * Workspace-level status cards and independently refreshed service observations.
 * 展示 Workspace 级状态卡片和分别刷新的服务观测信息。
 */
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

          <Panel
            title="Workspace blockers"
            meta={
              system?.blockers && system.blockers.length > 0 ? (
                <StatusPill value="BLOCKED" />
              ) : (
                <StatusPill value="READY" />
              )
            }
          >
            {system?.blockers && system.blockers.length > 0 ? (
              <div className="attention-list">
                {system.blockers.map((blocker) => (
                  <div className="attention-item" key={blocker.code}>
                    <span className="attention-item__icon" aria-hidden="true" style={{ color: "var(--red)", borderColor: "var(--red)" }}>!</span>
                    <div>
                      <strong style={{ color: "var(--red)" }}>{blocker.code}</strong>
                      <p>{blocker.message}</p>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <StateBlock
                kind="empty"
                title="No blockers detected"
                detail="All system requirements, GPU resources, disk space, and service dependencies are ready."
              />
            )}
          </Panel>

          <Panel
            title="Installed plugins"
            meta={<span className="mono-label">RUNTIME ENGINES</span>}
          >
            {system?.plugins && system.plugins.length > 0 ? (
              <div className="service-list">
                {system.plugins.map((plugin) => (
                  <div className="service-row" key={plugin.name}>
                    <span
                      className={`service-dot ${plugin.state === "READY" ? "service-dot--good" : "service-dot--bad"}`}
                      aria-hidden="true"
                    />
                    <div>
                      <strong>{plugin.name}</strong>
                      {plugin.kind ? <span style={{ marginLeft: "8px", color: "var(--muted)", fontSize: "11px" }}>({plugin.kind})</span> : null}
                    </div>
                    <StatusPill value={plugin.state} />
                  </div>
                ))}
              </div>
            ) : (
              <StateBlock
                kind="empty"
                title="No plugins detected"
                detail="No runtime plugins currently registered or reported by host."
              />
            )}
          </Panel>
        </div>
      )}
    </div>
  );
}

/**
 * Reactor model imports, binding choices, validation evidence, and import form.
 * 提供 Reactor 模型导入、binding 选择、校验依据和导入表单。
 */
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
  const [credentials, setCredentials] = useState<CredentialMetadata[] | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    void Promise.allSettled([
      api.getModelImports(),
      api.getServingBindings(),
      api.getCredentials(),
    ]).then(([modelResult, bindingResult, credentialResult]) => {
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
      // Credentials are metadata only; the select exposes names, never secrets.
      // 中文：凭据只保留元数据；选择框只显示名称，绝不显示密钥。
      setCredentials(credentialResult.status === "fulfilled" ? credentialResult.value : null);
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
          <Field
            label="Credential"
            hint="Optional. Private repositories need one; names are shown, secrets never are."
          >
            <select
              className="input-mono"
              value={credentialRef}
              onChange={(event) => setCredentialRef(event.target.value)}
            >
              <option value="">No credential (public repository)</option>
              {credentials?.map((credential) => (
                <option key={credential.id} value={credential.credentialRef}>
                  {credential.name} ({credential.provider})
                </option>
              ))}
            </select>
            {credentials === null ? (
              <span className="field__hint">
                Credentials could not be loaded — add one on the Settings page first.
              </span>
            ) : null}
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

/**
 * Catalyst dataset containers with a deliberately small create surface.
 * 提供有意保持精简创建流程的 Catalyst 数据集容器。
 */
/**
 * Media type to send when the browser reports none.
 * 浏览器未报告媒体类型时应发送的默认值。
 */
function contentTypeFor(filename: string): string {
  const suffix = filename.slice(filename.lastIndexOf(".")).toLowerCase();
  if (suffix === ".csv") return "text/csv";
  if (suffix === ".jsonl" || suffix === ".ndjson") return "application/x-ndjson";
  if (suffix === ".json") return "application/json";
  if (suffix === ".parquet") return "application/vnd.apache.parquet";
  return "text/plain";
}

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

  // Sample preview state
  // 中文：样本预览状态。
  const [previewVersionId, setPreviewVersionId] = useState("");
  const [previewLimit, setPreviewLimit] = useState(10);
  const [previewOffset, setPreviewOffset] = useState(0);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewData, setPreviewData] = useState<DatasetPreview | null>(null);

  // Preparation workflow: upload -> map -> confirm -> publish -> hand to Yield.
  // 中文：预处理流程：上传 → 映射 → 确认 → 发布 → 交给 Yield。
  const [selectedDatasetId, setSelectedDatasetId] = useState("");
  const [preparations, setPreparations] = useState<JsonRecord[] | null>(null);
  const [preparationId, setPreparationId] = useState("");
  const [uploadName, setUploadName] = useState("");
  const [workflowBusy, setWorkflowBusy] = useState(false);
  const [workflowError, setWorkflowError] = useState<string | null>(null);
  const [workflowNotice, setWorkflowNotice] = useState<string | null>(null);
  const [instructionField, setInstructionField] = useState("");
  const [inputField, setInputField] = useState("");
  const [outputField, setOutputField] = useState("");

  const detectedFields = (() => {
    const row = preparations?.find((item) => text(item["id"]) === preparationId);
    const raw = row?.["detectedFields"];
    return Array.isArray(raw) ? raw.filter((v): v is string => typeof v === "string") : [];
  })();

  const loadPreview = async (versionId: string, limit = 10, offset = 0) => {
    const vid = versionId.trim();
    if (!vid) {
      setPreviewError("Enter or select a DatasetVersion ID to preview samples.");
      return;
    }
    setPreviewVersionId(vid);
    setPreviewLoading(true);
    setPreviewError(null);
    try {
      const result = await api.getDatasetVersionPreview(vid, limit, offset);
      setPreviewData(result);
    } catch (err) {
      setPreviewError(errorMessage(err));
      setPreviewData(null);
    } finally {
      setPreviewLoading(false);
    }
  };

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

  const runWorkflow = async (label: string, action: () => Promise<JsonRecord>) => {
    setWorkflowBusy(true);
    setWorkflowError(null);
    setWorkflowNotice(null);
    try {
      const result = await action();
      const refreshed = await api.getPreparations(selectedDatasetId);
      setPreparations(refreshed);
      const nextId = text(result["id"], preparationId);
      if (nextId) setPreparationId(nextId);
      setWorkflowNotice(`${label} completed.${result["state"] ? ` State: ${text(result["state"])}.` : ""}`);
    } catch (workflowErr) {
      setWorkflowError(errorMessage(workflowErr));
    } finally {
      setWorkflowBusy(false);
    }
  };

  const onDatasetChosen = (id: string) => {
    setSelectedDatasetId(id);
    setPreparationId("");
    setPreparations(null);
    if (!id) return;
    void api.getPreparations(id).then(setPreparations, () => setPreparations(null));
  };

  const handleUpload = async (file: File) => {
    if (!selectedDatasetId) {
      setWorkflowError("Choose a dataset before uploading.");
      return;
    }
    setWorkflowBusy(true);
    setWorkflowError(null);
    setWorkflowNotice(null);
    try {
      // Catalyst reads the raw body, so the text is sent as-is. Browsers often
      // report no MIME type for .jsonl, and Catalyst derives CSV/Parquet from
      // the media type, so fall back to the suffix instead of assuming JSON.
      // 中文：Catalyst 读取原始请求正文，因此会原样发送文本。浏览器通常不会为 `.jsonl` 报告媒体类型；Catalyst 会根据媒体类型推断 CSV/Parquet，所以这里根据文件后缀回退，而不是假定内容一定是 JSON。
      const content = await file.text();
      const created = await api.createPreparation(
        selectedDatasetId,
        uploadName.trim() || file.name,
        file.name,
        content,
        file.type || contentTypeFor(file.name),
      );
      const refreshed = await api.getPreparations(selectedDatasetId);
      setPreparations(refreshed);
      setPreparationId(text(created["id"], ""));
      setWorkflowNotice(`Uploaded ${file.name}. Map the detected fields next.`);
    } catch (uploadError) {
      setWorkflowError(errorMessage(uploadError));
    } finally {
      setWorkflowBusy(false);
    }
  };

  const handleMap = () =>
    runWorkflow("Mapping", () =>
      api.configurePreparationMapping(preparationId, {
        mapping: {
          mode: "instruction",
          instruction: instructionField ? { field: instructionField } : null,
          input: inputField ? { field: inputField } : null,
          output: outputField ? { field: outputField } : null,
        },
        normalization: { trimWhitespace: true, collapseWhitespace: true, unicodeNfc: true },
      }),
    );

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

      <Panel title="Prepare data" meta={<span className="mono-label">UPLOAD → MAP → PREPARE → PUBLISH</span>}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: "16px" }}>
          <Field label="Dataset" hint="Preparation always belongs to one dataset.">
            <select
              className="input-mono"
              value={selectedDatasetId}
              onChange={(event) => onDatasetChosen(event.target.value)}
            >
              <option value="">Select a dataset</option>
              {datasets?.map((row, index) => (
                <option key={text(row["id"], `dataset-${index}`)} value={text(row["id"], "")}>
                  {text(row["name"], "Unnamed dataset")}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Preparation name" hint="Optional; defaults to the filename.">
            <input
              className="input-mono"
              value={uploadName}
              onChange={(event) => setUploadName(event.target.value)}
              placeholder="instruction-tuning-v1"
            />
          </Field>

          <Field label="Upload source file" hint="JSONL / JSON / CSV / TEXT. The file is sent as the request body.">
            <input
              type="file"
              accept=".jsonl,.json,.csv,.txt,application/json,text/csv,text/plain"
              disabled={!selectedDatasetId || workflowBusy}
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void handleUpload(file);
                event.target.value = "";
              }}
            />
          </Field>
        </div>

        <Field label="Preparation" hint="Pick the uploaded preparation to map and publish.">
          <select
            className="input-mono"
            value={preparationId}
            onChange={(event) => setPreparationId(event.target.value)}
            disabled={!preparations?.length}
          >
            <option value="">
              {preparations ? (preparations.length ? "Select a preparation" : "No preparations yet") : "Choose a dataset first"}
            </option>
            {preparations?.map((row, index) => (
              <option key={text(row["id"], `prep-${index}`)} value={text(row["id"], "")}>
                {`${text(row["name"], "unnamed")} — ${text(row["state"], "UNKNOWN")}`}
              </option>
            ))}
          </select>
        </Field>

        {preparationId ? (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: "16px" }}>
            <Field label="Instruction field" hint="Detected columns from the upload.">
              <select className="input-mono" value={instructionField} onChange={(event) => setInstructionField(event.target.value)}>
                <option value="">(none)</option>
                {detectedFields.map((field) => (
                  <option key={field} value={field}>{field}</option>
                ))}
              </select>
            </Field>
            <Field label="Input field" hint="Optional context column.">
              <select className="input-mono" value={inputField} onChange={(event) => setInputField(event.target.value)}>
                <option value="">(none)</option>
                {detectedFields.map((field) => (
                  <option key={field} value={field}>{field}</option>
                ))}
              </select>
            </Field>
            <Field label="Output field" hint="Target completion column.">
              <select className="input-mono" value={outputField} onChange={(event) => setOutputField(event.target.value)}>
                <option value="">(none)</option>
                {detectedFields.map((field) => (
                  <option key={field} value={field}>{field}</option>
                ))}
              </select>
            </Field>
          </div>
        ) : null}

        <div className="form-actions">
          <Button tone="primary" disabled={!preparationId || workflowBusy} onClick={() => void handleMap()}>
            {workflowBusy ? "Working..." : "Save mapping"}
          </Button>
          <Button
            disabled={!preparationId || workflowBusy}
            onClick={() => void runWorkflow("Preparation", () => api.confirmPreparation(preparationId))}
          >
            Prepare
          </Button>
          <Button
            disabled={!preparationId || workflowBusy}
            onClick={() => void runWorkflow("Publish", () => api.publishPreparation(preparationId))}
          >
            Publish version
          </Button>
          <Button
            disabled={!preparationId || workflowBusy}
            onClick={() => void runWorkflow("Handoff", () => api.sendPreparationToYield(preparationId))}
          >
            Send to Yield
          </Button>
        </div>
        {workflowError ? <p className="inline-error" role="alert">{workflowError}</p> : null}
        {workflowNotice ? <p className="form-message form-message--success">{workflowNotice}</p> : null}
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
              {
                label: "Action",
                render: (row) => {
                  const id = text(row["id"] || row["datasetId"]);
                  return (
                    <Button
                      onClick={() => {
                        setPreviewVersionId(id);
                        void loadPreview(id, previewLimit, previewOffset);
                      }}
                    >
                      预览样本
                    </Button>
                  );
                },
              },
            ]}
          />
        ) : (
          <StateBlock kind="empty" title="No dataset containers" detail="Create the container that will own your next preparation flow." />
        )}
      </Panel>

      <Panel
        title="Dataset version sample preview"
        meta={previewData ? `${previewData.totalRows} total rows` : "CATALYST DUCKDB"}
      >
        <form
          className="form-grid form-grid--compact"
          onSubmit={(e) => {
            e.preventDefault();
            void loadPreview(previewVersionId, previewLimit, previewOffset);
          }}
        >
          <div style={{ display: "flex", gap: "12px", alignItems: "flex-end", flexWrap: "wrap" }}>
            <div style={{ flex: "1 1 300px" }}>
              <Field label="Dataset version ID" hint="UUID of the prepared dataset version">
                <input
                  className="input-mono"
                  value={previewVersionId}
                  onChange={(e) => setPreviewVersionId(e.target.value)}
                  placeholder="e.g. 11111111-2222-3333-4444-555555555555"
                />
              </Field>
            </div>
            <div style={{ width: "100px" }}>
              <Field label="Limit">
                <select
                  value={previewLimit}
                  onChange={(e) => setPreviewLimit(Number(e.target.value))}
                >
                  <option value={5}>5</option>
                  <option value={10}>10</option>
                  <option value={20}>20</option>
                </select>
              </Field>
            </div>
            <div style={{ width: "100px" }}>
              <Field label="Offset">
                <input
                  type="number"
                  min="0"
                  value={previewOffset}
                  onChange={(e) => setPreviewOffset(Math.max(0, parseInt(e.target.value, 10) || 0))}
                  className="input-mono"
                />
              </Field>
            </div>
            <div>
              <Button
                tone="primary"
                type="submit"
                disabled={previewLoading || !previewVersionId.trim()}
              >
                {previewLoading ? "Loading..." : "预览样本"}
              </Button>
            </div>
          </div>
        </form>

        {previewLoading ? (
          <StateBlock
            kind="loading"
            title="Loading sample preview"
            detail="Catalyst DuckDB is reading sample rows from the CAS parquet/jsonl artifact."
          />
        ) : previewError ? (
          <StateBlock kind="error" title="Preview unavailable" detail={previewError} />
        ) : previewData ? (
          <div style={{ marginTop: "16px" }}>
            <p style={{ fontSize: "12px", color: "var(--muted)", marginBottom: "12px" }}>
              Displaying {previewData.rows.length} rows (out of {previewData.totalRows} total rows) for version <code className="input-mono">{previewData.versionId}</code>
            </p>
            <div style={{ display: "grid", gap: "12px" }}>
              {previewData.rows.map((row) => (
                <div
                  key={row.index}
                  style={{
                    background: "rgba(17, 29, 34, 0.6)",
                    border: "1px solid var(--line)",
                    borderRadius: "6px",
                    padding: "12px",
                  }}
                >
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: "8px" }}>
                    <span className="mono-label">ROW #{row.index + 1}</span>
                  </div>
                  <div style={{ display: "grid", gap: "6px", fontSize: "13px" }}>
                    {row.mapped["instruction"] !== undefined ? (
                      <div>
                        <strong style={{ color: "var(--blue)" }}>Instruction: </strong>
                        <span>{String(row.mapped["instruction"])}</span>
                      </div>
                    ) : null}
                    {row.mapped["input"] ? (
                      <div>
                        <strong style={{ color: "var(--muted)" }}>Input: </strong>
                        <span>{String(row.mapped["input"])}</span>
                      </div>
                    ) : null}
                    {row.mapped["output"] !== undefined ? (
                      <div>
                        <strong style={{ color: "var(--lime)" }}>Output: </strong>
                        <span>{String(row.mapped["output"])}</span>
                      </div>
                    ) : null}
                  </div>
                  <details style={{ marginTop: "8px", fontSize: "12px", color: "var(--faint)" }}>
                    <summary style={{ cursor: "pointer", userSelect: "none" }}>Raw record JSON</summary>
                    <pre
                      style={{
                        background: "var(--ink-soft)",
                        padding: "8px",
                        borderRadius: "4px",
                        overflowX: "auto",
                        marginTop: "6px",
                        color: "var(--text)",
                        fontFamily: "var(--mono)",
                        fontSize: "11px",
                      }}
                    >
                      {JSON.stringify(row.raw, null, 2)}
                    </pre>
                  </details>
                </div>
              ))}
            </div>
          </div>
        ) : (
          <StateBlock
            kind="empty"
            title="No samples loaded"
            detail="Enter a version ID above or click preview on a dataset container to inspect mapped samples."
          />
        )}
      </Panel>
    </div>
  );
}

export interface ParamFieldProps {
  label: string;
  hint: string;
  /**
   * Omitted for fields that are not LLaMA Factory parameters, such as pickers.
   * 对于非 LLaMA Factory 参数（例如选择器），此属性会省略。
   */
  llamaKey?: string;
  children: React.ReactNode;
}

/**
 * Parameter field with user-friendly hint and toggleable LLaMA Factory key.
 * 参数字段：显示用户友好提示，并可切换 LLaMA Factory 参数键。
 */
export function ParamField({ label, hint, llamaKey, children }: ParamFieldProps) {
  const [showKey, setShowKey] = useState(false);
  return (
    <div className="field">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "4px" }}>
        <span className="field__label">{label}</span>
        {llamaKey ? (
          <button
            type="button"
            className="button button--quiet"
            style={{ padding: "2px 6px", fontSize: "11px", height: "auto" }}
            onClick={() => setShowKey((v) => !v)}
          >
            {showKey ? "Hide LLaMA key" : "LLaMA Factory key"}
          </button>
        ) : null}
      </div>
      {children}
      <span className="field__hint">
        {hint}
        {showKey && llamaKey && (
          <code style={{ marginLeft: "8px", color: "var(--lime)", fontFamily: "var(--mono)" }}>
            ({llamaKey})
          </code>
        )}
      </span>
    </div>
  );
}

/**
 * Read a prepared draft's base model so a relaunch does not have to re-choose one.
 * 读取已准备草稿的基础模型，使重新启动时无需再次选择模型。
 */
function draftBaseModel(row: JsonRecord): JsonRecord | null {
  const configuration = row["configuration"];
  if (typeof configuration !== "object" || configuration === null) {
    return null;
  }
  const baseModel = (configuration as JsonRecord)["baseModel"];
  if (typeof baseModel !== "object" || baseModel === null) {
    return null;
  }
  return baseModel as JsonRecord;
}

/**
 * Training draft list with explicit launch actions and hyperparameter reference.
 * 展示训练草稿列表、明确的启动操作和超参数参考。
 */
export function TrainingPage({ api }: PageProps) {
  const [reloadKey, setReloadKey] = useState(0);
  const [drafts, setDrafts] = useState<JsonRecord[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionId, setActionId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Hyperparameter fields state
  // 中文：超参数字段状态。
  const [epochs, setEpochs] = useState("3");
  const [learningRate, setLearningRate] = useState("0.0002");
  const [batchSize, setBatchSize] = useState("2");
  const [gradAccum, setGradAccum] = useState("4");
  const [cutoffLen, setCutoffLen] = useState("2048");
  const [loraRank, setLoraRank] = useState("8");
  const [loraAlpha, setLoraAlpha] = useState("16");
  const [loraDropout, setLoraDropout] = useState("0.05");

  // Picker sources: a draft is launched from a chosen dataset version and base
  // model rather than from identifiers typed by hand.
  // 中文：选择器的数据来源：启动草稿时选择数据集版本和基础模型，不要求用户手动输入标识。
  const [datasets, setDatasets] = useState<JsonRecord[] | null>(null);
  const [datasetId, setDatasetId] = useState("");
  const [versions, setVersions] = useState<JsonRecord[] | null>(null);
  const [versionId, setVersionId] = useState("");
  const [modelImports, setModelImports] = useState<JsonRecord[] | null>(null);
  const [baseModelId, setBaseModelId] = useState("");

  useEffect(() => {
    let active = true;
    void api.getDatasets().then(
      (value) => {
        if (active) setDatasets(value);
      },
      () => {
        if (active) setDatasets(null);
      },
    );
    void api.getModelImports().then(
      (value) => {
        if (active) setModelImports(value);
      },
      () => {
        if (active) setModelImports(null);
      },
    );
    return () => {
      active = false;
    };
  }, [api]);

  useEffect(() => {
    if (!datasetId) {
      setVersions(null);
      setVersionId("");
      return;
    }
    let active = true;
    setVersions(null);
    setVersionId("");
    void api.getDatasetVersions(datasetId).then(
      (value) => {
        if (!active) return;
        setVersions(value);
        const published = value.find((row) => text(row["state"]) === "PUBLISHED");
        setVersionId(text((published ?? value[0])?.["id"], ""));
      },
      () => {
        if (active) setVersions(null);
      },
    );
    return () => {
      active = false;
    };
  }, [api, datasetId]);

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

  /**
   * The chosen base model, but only when Reactor published everything Yield
   * needs. `PrepareTrainingDraft.baseModel` requires both a portable model
   * artifact and a pinned {repository, revision} source, and the model is
   * declared with extra="forbid", so a partial payload would be rejected.
   * 仅当 Reactor 已发布 Yield 所需的全部信息时，才提供所选基础模型。`PrepareTrainingDraft.baseModel` 同时要求可移植模型制品和已固定的 `{repository, revision}` 来源；模型声明了 `extra="forbid"`，因此不完整的负载会被拒绝。
   */
  const selectedBaseModel = (() => {
    if (!baseModelId || !modelImports) return null;
    const row = modelImports.find((candidate) => text(candidate["id"]) === baseModelId);
    if (!row) return null;
    const artifact = row["artifact"];
    const source = row["source"];
    if (typeof artifact !== "object" || artifact === null) return null;
    if (typeof source !== "object" || source === null) return null;
    const typedSource = source as JsonRecord;
    if (!text(typedSource["repository"], "") || !text(typedSource["revision"], "")) return null;
    return { artifact: artifact as JsonRecord, source: typedSource } as JsonRecord;
  })();

  const collectParameters = (): TrainingParametersInput => ({
    epochs: Number(epochs),
    perDeviceBatchSize: Number(batchSize),
    gradientAccumulationSteps: Number(gradAccum),
    learningRate: Number(learningRate),
    maxSequenceLength: Number(cutoffLen),
    loraRank: Number(loraRank),
    loraAlpha: Number(loraAlpha),
    loraDropout: Number(loraDropout),
  });

  /**
   * Persist the edited hyperparameters to Yield, then launch.
   *
   * Launching without the PATCH silently trains with whatever the draft already
   * carried, so every value edited here would be discarded with no error.
   * 先将编辑后的超参数持久化到 Yield，再启动训练。
   * 如果没有 PATCH 就启动，训练会静默使用草稿原有的参数，因此这里编辑的所有值都会无错误地丢失。
   */
  const startDraft = async (id: string, row: JsonRecord) => {
    setActionId(id);
    setActionError(null);
    try {
      const parameters = collectParameters();
      const invalid = Object.entries(parameters)
        .filter(([, value]) => !Number.isFinite(value))
        .map(([name]) => name);
      if (invalid.length > 0) {
        throw new Error(`Invalid hyperparameter value(s): ${invalid.join(", ")}`);
      }
      const baseModel = selectedBaseModel ?? draftBaseModel(row);
      if (!baseModel) {
        throw new Error("This draft has no base model configured; prepare it in Yield first.");
      }
      await api.updateTrainingDraft(id, { baseModel, parameters });
      await api.startTrainingDraft(id);
      setReloadKey((value) => value + 1);
    } catch (launchError) {
      setActionError(errorMessage(launchError));
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

      <Panel
        title="Training hyperparameters & LLaMA Factory mapping"
        meta={<span className="mono-label">LLAMA-FACTORY ENGINE</span>}
      >
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: "16px" }}>
          <ParamField
            label="Epochs / 训练轮数"
            hint="完整遍历数据集的次数"
            llamaKey="num_train_epochs"
          >
            <input
              type="number"
              min="1"
              step="1"
              value={epochs}
              onChange={(e) => setEpochs(e.target.value)}
              className="input-mono"
            />
          </ParamField>

          <ParamField
            label="Learning rate / 学习率"
            hint="每步权重更新幅度"
            llamaKey="learning_rate"
          >
            <input
              type="text"
              value={learningRate}
              onChange={(e) => setLearningRate(e.target.value)}
              className="input-mono"
            />
          </ParamField>

          <ParamField
            label="Batch size / 批次大小"
            hint="每步处理的样本数（越大越快但 VRAM 更多）"
            llamaKey="per_device_train_batch_size"
          >
            <input
              type="number"
              min="1"
              step="1"
              value={batchSize}
              onChange={(e) => setBatchSize(e.target.value)}
              className="input-mono"
            />
          </ParamField>

          <ParamField
            label="Gradient accumulation / 梯度累积"
            hint="累积梯度的步数（等效放大 batch size）"
            llamaKey="gradient_accumulation_steps"
          >
            <input
              type="number"
              min="1"
              step="1"
              value={gradAccum}
              onChange={(e) => setGradAccum(e.target.value)}
              className="input-mono"
            />
          </ParamField>

          <ParamField
            label="Sequence length / 截断长度"
            hint="单条样本最大 token 数"
            llamaKey="cutoff_len"
          >
            <input
              type="number"
              min="128"
              step="128"
              value={cutoffLen}
              onChange={(e) => setCutoffLen(e.target.value)}
              className="input-mono"
            />
          </ParamField>

          <ParamField
            label="LoRA rank / LoRA 秩"
            hint="LoRA 矩阵秩（越大参数越多）"
            llamaKey="lora_rank"
          >
            <input
              type="number"
              min="1"
              step="1"
              value={loraRank}
              onChange={(e) => setLoraRank(e.target.value)}
              className="input-mono"
            />
          </ParamField>

          <ParamField
            label="LoRA alpha / LoRA 缩放系数"
            hint="LoRA 缩放因子（通常 = 2 × rank）"
            llamaKey="lora_alpha"
          >
            <input
              type="number"
              min="1"
              step="1"
              value={loraAlpha}
              onChange={(e) => setLoraAlpha(e.target.value)}
              className="input-mono"
            />
          </ParamField>

          <ParamField
            label="LoRA dropout / LoRA 丢弃率"
            hint="LoRA 层丢弃率（防过拟合）"
            llamaKey="lora_dropout"
          >
            <input
              type="text"
              value={loraDropout}
              onChange={(e) => setLoraDropout(e.target.value)}
              className="input-mono"
            />
          </ParamField>
        </div>
        <p className="field__hint">
          These values are submitted to Yield when you press <strong>Save and start</strong> on a
          draft. Nothing here edits anything until that button is used.
        </p>
      </Panel>

      <Panel title="Choose the training inputs" meta="PICKERS, NOT IDENTIFIERS">
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: "16px" }}>
          <ParamField label="Dataset" hint="Catalyst owns preparation and publishing.">
            <select
              className="input-mono"
              value={datasetId}
              onChange={(event) => setDatasetId(event.target.value)}
            >
              <option value="">{datasets ? "Select a dataset" : "Loading datasets..."}</option>
              {datasets?.map((row, index) => (
                <option key={text(row["id"], `dataset-${index}`)} value={text(row["id"], "")}>
                  {text(row["name"], "Unnamed dataset")}
                </option>
              ))}
            </select>
          </ParamField>

          <ParamField label="Dataset version" hint="Published versions only; newest first.">
            <select
              className="input-mono"
              value={versionId}
              onChange={(event) => setVersionId(event.target.value)}
              disabled={!datasetId}
            >
              <option value="">
                {!datasetId
                  ? "Choose a dataset first"
                  : versions
                    ? "Select a version"
                    : "Loading versions..."}
              </option>
              {versions?.map((row, index) => (
                <option key={text(row["id"], `version-${index}`)} value={text(row["id"], "")}>
                  {`v${text(row["version"], "?")} — ${text(row["state"], "UNKNOWN")}`}
                </option>
              ))}
            </select>
          </ParamField>

          <ParamField
            label="Base model"
            hint="Selecting one overrides the draft's current base model at launch."
          >
            <select
              className="input-mono"
              value={baseModelId}
              onChange={(event) => setBaseModelId(event.target.value)}
            >
              <option value="">{modelImports ? "Keep the draft's base model" : "Loading models..."}</option>
              {modelImports?.map((row, index) => (
                <option key={text(row["id"], `model-${index}`)} value={text(row["id"], "")}>
                  {text(row["name"], text(row["id"], `model-${index}`))}
                </option>
              ))}
            </select>
          </ParamField>
        </div>
        {baseModelId && !selectedBaseModel ? (
          <p className="inline-error" role="alert">
            This model import does not expose a portable artifact and source yet, so the draft's
            existing base model will be used.
          </p>
        ) : null}
      </Panel>

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
                    const baseModel = draftBaseModel(row);
                    const launchable = prepared && baseModel !== null;
                    const busy = actionId === id;
                    return (
                      <Button
                        tone="primary"
                        disabled={!launchable || actionId !== null}
                        onClick={() => void startDraft(id, row)}
                        title={
                          !prepared
                            ? "Prepare this draft in Yield first"
                            : !baseModel
                              ? "This draft has no base model; prepare it in Yield first"
                              : "Save these hyperparameters to Yield, then start the run"
                        }
                      >
                        {busy ? "Saving and launching..." : launchable ? "Save and start" : "Not ready"}
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

/**
 * Run lookup and attempt diagnostics, reflecting the published Yield API shape.
 * 展示运行查询和尝试诊断信息，并遵循已发布的 Yield API 结构。
 */
/**
 * Minimal canvas loss curve so the console needs no charting dependency.
 * 使用最精简的 canvas 绘制损失曲线，使控制台无需引入图表依赖。
 */
function LossChart({ series }: { series: number[] }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || series.length < 2) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    const { width, height } = canvas;
    context.clearRect(0, 0, width, height);
    const min = Math.min(...series);
    const max = Math.max(...series);
    const span = max - min || 1;
    context.beginPath();
    series.forEach((value, index) => {
      const x = (index / (series.length - 1)) * (width - 2) + 1;
      const y = height - 1 - ((value - min) / span) * (height - 2);
      if (index === 0) context.moveTo(x, y);
      else context.lineTo(x, y);
    });
    context.strokeStyle = "#7dd3fc";
    context.lineWidth = 1.5;
    context.stroke();
  }, [series]);

  if (series.length < 2) return null;
  return (
    <canvas
      ref={canvasRef}
      width={480}
      height={120}
      style={{ width: "100%", height: "120px" }}
      aria-label="Training loss over steps"
    />
  );
}

/**
 * Human-readable duration for an ETA in seconds.
 * 将 ETA 秒数格式化为人类可读的时长。
 */
function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

export function RunsPage({ api }: PageProps) {
  const [runId, setRunId] = useState(() => initialRunId());
  const [run, setRun] = useState<JsonRecord | null>(null);
  const [attempts, setAttempts] = useState<JsonRecord[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attemptError, setAttemptError] = useState<string | null>(null);
  const [canceling, setCanceling] = useState(false);

  // SSE Realtime events stream state
  // 中文：SSE 实时事件流状态。
  const [events, setEvents] = useState<JsonRecord[]>([]);
  const [latestLoss, setLatestLoss] = useState<number | null>(null);
  const [currentStep, setCurrentStep] = useState<number | null>(null);
  const [totalSteps, setTotalSteps] = useState<number | null>(null);
  const [streamActive, setStreamActive] = useState(false);
  const [streamDone, setStreamDone] = useState(false);
  const [lossSeries, setLossSeries] = useState<number[]>([]);
  const [etaSeconds, setEtaSeconds] = useState<number | null>(null);
  const [latestCheckpoint, setLatestCheckpoint] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionNotice, setActionNotice] = useState<string | null>(null);

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
  const resultRecord = (run?.["result"] ?? null) as JsonRecord | null;
  const resultId = resultRecord ? text(resultRecord["id"], "") : "";
  // Resume needs both a terminal failure state and the checkpoint to resume from.
  // 中文：恢复运行必须同时具备终态失败状态和待恢复的检查点。
  const canResume = ["FAILED", "CANCELLED"].includes(runState) && latestCheckpoint !== null;
  const canDeploy = Boolean(resultId);

  const resumeRun = async () => {
    if (!currentRunId) return;
    setActionBusy(true);
    setActionNotice(null);
    try {
      await api.resumeTrainingRun(currentRunId, latestCheckpoint ?? undefined);
      setActionNotice("Resume accepted. Yield is restarting from the latest checkpoint.");
      setStreamDone(false);
      setRun(await api.getTrainingRun(currentRunId));
    } catch (resumeError) {
      setError(errorMessage(resumeError));
    } finally {
      setActionBusy(false);
    }
  };

  const deployResult = async () => {
    if (!resultId) return;
    setActionBusy(true);
    setActionNotice(null);
    try {
      await api.sendResultToReactor(resultId);
      setActionNotice("Sent to Reactor. Continue on the Deployments page.");
    } catch (deployError) {
      setError(errorMessage(deployError));
    } finally {
      setActionBusy(false);
    }
  };

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
                  const loss = payload["loss"];
                  setLatestLoss(loss);
                  setLossSeries((prev) => [...prev.slice(-199), loss]);
                }
                const step = payload["step"] ?? payload["currentStep"] ?? payload["current_step"];
                if (typeof step === "number") {
                  setCurrentStep(Number(step));
                }
                const total = payload["totalSteps"] ?? payload["total_steps"] ?? payload["total"];
                if (typeof total === "number") {
                  setTotalSteps(Number(total));
                }
                const eta = payload["etaSeconds"] ?? payload["eta_seconds"];
                if (typeof eta === "number") {
                  setEtaSeconds(eta);
                }
                const checkpoint = payload["checkpoint"];
                if (checkpoint && typeof checkpoint === "object") {
                  const record = checkpoint as Record<string, unknown>;
                  const label =
                    record["name"] ?? record["checkpointName"] ?? record["artifact"] ?? record["digest"];
                  if (typeof label === "string") {
                    setLatestCheckpoint(label);
                  } else if (typeof label === "object" && label !== null) {
                    const digest = (label as Record<string, unknown>)["digest"];
                    if (typeof digest === "string") setLatestCheckpoint(digest);
                  }
                }
              } catch {
                // Ignore parse errors
                // 中文：忽略解析错误。
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
              <MetricCard
                label="ETA"
                value={etaSeconds !== null ? formatDuration(etaSeconds) : "--"}
                detail="Estimated time remaining"
                accent="gray"
              />
              <MetricCard
                label="Checkpoint"
                value={latestCheckpoint ?? "--"}
                detail="Latest checkpoint reported by Yield"
                accent="gray"
              />
            </div>

            {currentStep !== null && totalSteps ? (
              <div style={{ marginTop: "16px" }}>
                <div
                  role="progressbar"
                  aria-valuenow={currentStep}
                  aria-valuemin={0}
                  aria-valuemax={totalSteps}
                  style={{ height: "8px", background: "var(--color-bg-subtle, #181c20)", borderRadius: "4px", overflow: "hidden" }}
                >
                  <div
                    style={{
                      width: `${Math.min(100, Math.round((currentStep / totalSteps) * 100))}%`,
                      height: "100%",
                      background: "var(--lime, #7dd3fc)",
                    }}
                  />
                </div>
              </div>
            ) : null}

            <div style={{ marginTop: "16px" }}>
              <LossChart series={lossSeries} />
            </div>

            <div className="form-actions" style={{ marginTop: "16px" }}>
              <Button disabled={!canResume || actionBusy} onClick={() => void resumeRun()}>
                {actionBusy ? "Working..." : "Resume from checkpoint"}
              </Button>
              <Button disabled={!canDeploy || actionBusy} onClick={() => void deployResult()}>
                Deploy this model
              </Button>
            </div>
            {actionNotice ? <p className="form-message form-message--success">{actionNotice}</p> : null}

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

/**
 * Reactor deployment intent, observed state, and explicit stop action.
 * 展示 Reactor 部署意图、观测状态和明确的停止操作。
 */
export function DeploymentsPage({ api }: PageProps) {
  const [reloadKey, setReloadKey] = useState(0);
  const [deployments, setDeployments] = useState<JsonRecord[] | null>(null);
  const [bindings, setBindings] = useState<JsonRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [bindingError, setBindingError] = useState<string | null>(null);
  const [stopId, setStopId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Deployment events timeline state
  // 中文：部署事件时间线状态。
  const [selectedDeploymentId, setSelectedDeploymentId] = useState<string | null>(null);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventsError, setEventsError] = useState<string | null>(null);
  const [deploymentEvents, setDeploymentEvents] = useState<DeploymentEvent[] | null>(null);

  const loadDeploymentEvents = async (id: string) => {
    setSelectedDeploymentId(id);
    setEventsLoading(true);
    setEventsError(null);
    try {
      const res = await api.getDeploymentEvents(id);
      setDeploymentEvents(res.events);
    } catch (err) {
      setEventsError(errorMessage(err));
      setDeploymentEvents(null);
    } finally {
      setEventsLoading(false);
    }
  };

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
                  label: "Timeline",
                  render: (row) => {
                    const id = text(row["id"], "");
                    const isSelected = selectedDeploymentId === id;
                    return (
                      <Button
                        tone={isSelected ? "primary" : "quiet"}
                        onClick={() => void loadDeploymentEvents(id)}
                      >
                        加载历史
                      </Button>
                    );
                  },
                },
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

      {selectedDeploymentId ? (
        <Panel
          title={`加载历史时间线: ${selectedDeploymentId}`}
          meta={
            deploymentEvents ? (
              <span className="mono-label">{deploymentEvents.length} EVENTS</span>
            ) : undefined
          }
        >
          {eventsLoading ? (
            <StateBlock
              kind="loading"
              title="Loading deployment phase events"
              detail="Reactor SQLite store is returning the phase transition timeline."
            />
          ) : eventsError ? (
            <StateBlock kind="error" title="Events unavailable" detail={eventsError} />
          ) : deploymentEvents && deploymentEvents.length > 0 ? (
            <div style={{ marginTop: "12px" }}>
              {/* Phase sequence visualization */}
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "8px",
                  marginBottom: "20px",
                  overflowX: "auto",
                  padding: "8px 0",
                }}
              >
                {["QUEUED", "LOADING", "PROBING", "READY"].map((p, idx) => {
                  const hasPassed = deploymentEvents.some((e) => e.phase === p);
                  return (
                    <div key={p} style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                      <span
                        style={{
                          padding: "4px 10px",
                          borderRadius: "12px",
                          fontSize: "12px",
                          fontWeight: 600,
                          fontFamily: "var(--mono)",
                          background: hasPassed ? "rgba(201, 242, 123, 0.15)" : "rgba(255, 255, 255, 0.05)",
                          color: hasPassed ? "var(--lime)" : "var(--faint)",
                          border: `1px solid ${hasPassed ? "var(--lime)" : "var(--line)"}`,
                        }}
                      >
                        {p}
                      </span>
                      {idx < 3 ? <span style={{ color: "var(--faint)" }}>→</span> : null}
                    </div>
                  );
                })}
                {deploymentEvents.some((e) => e.phase === "FAILED") ? (
                  <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                    <span style={{ color: "var(--red)" }}>→</span>
                    <span
                      style={{
                        padding: "4px 10px",
                        borderRadius: "12px",
                        fontSize: "12px",
                        fontWeight: 600,
                        fontFamily: "var(--mono)",
                        background: "rgba(255, 139, 120, 0.15)",
                        color: "var(--red)",
                        border: "1px solid var(--red)",
                      }}
                    >
                      FAILED
                    </span>
                  </div>
                ) : null}
              </div>

              {/* Detailed event timeline list */}
              <div className="service-list">
                {deploymentEvents.map((evt) => (
                  <div
                    key={evt.sequence}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "40px 100px 1fr auto",
                      alignItems: "center",
                      gap: "12px",
                      padding: "10px 0",
                      borderBottom: "1px solid var(--line)",
                      fontSize: "12px",
                    }}
                  >
                    <span className="mono-label">#{evt.sequence}</span>
                    <StatusPill value={evt.phase} />
                    <div>
                      <span>{evt.message}</span>
                      {evt.failureCode ? (
                        <span
                          style={{
                            display: "block",
                            color: "var(--red)",
                            fontWeight: 600,
                            marginTop: "2px",
                            fontFamily: "var(--mono)",
                          }}
                        >
                          Failure: {evt.failureCode}
                        </span>
                      ) : null}
                    </div>
                    <span style={{ color: "var(--muted)", fontSize: "11px" }}>
                      {formatDate(evt.occurredAt)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <StateBlock
              kind="empty"
              title="No events recorded"
              detail="Reactor has not published loading events for this deployment yet."
            />
          )}
        </Panel>
      ) : null}
    </div>
  );
}

/**
 * Exchange Gateway routes, invocation snippets, and API key lifecycle administration.
 * 管理 Exchange Gateway 路由、调用示例和 API key 生命周期。
 */
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
  // 中文：新建 API key 表单状态。
  const [keyName, setKeyName] = useState("");
  const [expiresDays, setExpiresDays] = useState("");
  const [modelScope, setModelScope] = useState("");
  const [creatingKey, setCreatingKey] = useState(false);
  const [createdSecret, setCreatedSecret] = useState<string | null>(null);
  const [createdKeyName, setCreatedKeyName] = useState<string | null>(null);
  const [copiedKey, setCopiedKey] = useState(false);
  const [copiedSnippet, setCopiedSnippet] = useState(false);
  const [copiedBaseUrl, setCopiedBaseUrl] = useState(false);
  const [system, setSystem] = useState<SystemStatus | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    void Promise.allSettled([
      api.getGatewayRoutes(),
      api.listApiKeys(),
      api.getSystemStatus(),
    ]).then(([routesRes, keysRes, systemRes]) => {
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

      // A missing status only costs us the published gateway URL; the route and
      // key panels above must still render.
      // 中文：状态缺失只会导致无法显示已发布的 Gateway URL；上方的路由和密钥面板仍必须正常显示。
      setSystem(systemRes.status === "fulfilled" ? systemRes.value : null);
      setLoading(false);
    });
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

  // The published gateway URL wins. Deriving it in the browser assumed Exchange
  // sits on one fixed port, which is wrong for the dev stack (8000) and for any
  // HTTPS deployment, so every snippet below was pointing at a dead endpoint.
  // 中文：优先使用已发布的 Gateway URL。若在浏览器中自行推导地址，就等于假设 Exchange 固定使用一个端口；开发环境端口为 8000，HTTPS 部署也不同，因此下方所有调用示例都会指向失效 Endpoint。
  const baseUrl =
    system?.gatewayBaseUrl ??
    (typeof window !== "undefined"
      ? `${window.location.protocol}//${window.location.hostname}:8003/v1`
      : "http://localhost:8003/v1");

  const handleUseInNavigator = async (route: JsonRecord) => {
    const gatewayEndpointId = text(route["gatewayEndpointId"] || route["gateway_endpoint_id"] || route["id"]);
    const model = text(route["modelPattern"] || route["model_pattern"] || route["targetModel"] || "default-model");
    try {
      await api.setActiveRoute({
        gatewayEndpointId,
        modelId: model,
        baseUrl,
        apiKeyHint: text(route["name"] || model),
      });
      pushRoute("chat");
    } catch (err) {
      setActionError(errorMessage(err));
    }
  };

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
                    <div style={{ display: "flex", gap: "6px" }}>
                      <Button onClick={() => setSelectedRoute(row)}>
                        View detail
                      </Button>
                      <Button tone="primary" onClick={() => void handleUseInNavigator(row)}>
                        在 Navigator 中使用
                      </Button>
                    </div>
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

          <div style={{ marginTop: "12px", marginBottom: "16px", display: "flex", gap: "8px" }}>
            <Button
              onClick={() => {
                void navigator.clipboard.writeText(baseUrl);
                setCopiedBaseUrl(true);
                setTimeout(() => setCopiedBaseUrl(false), 2000);
              }}
            >
              {copiedBaseUrl ? "Base URL Copied!" : "Copy Base URL"}
            </Button>
            <Button
              tone="primary"
              onClick={() => void handleUseInNavigator(selectedRoute)}
            >
              在 Navigator 中使用
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

/**
 * Web Host session metadata and write-only credential administration.
 * 展示 Web Host 会话元数据并管理只写凭据。
 */
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

/**
 * Interactive test chat surface bound to the session's active Gateway route.
 * 提供绑定到当前会话活动 Gateway 路由的交互式测试聊天界面。
 */
export function ChatPage({ api }: PageProps) {
  const [activeRoute, setActiveRoute] = useState<ActiveRoutePayload | null>(null);
  const [loadingRoute, setLoadingRoute] = useState(true);
  const [routeError, setRouteError] = useState<string | null>(null);

  // API Key stored in sessionStorage ONLY (never localStorage or server)
  // 中文：API key 仅存储在 sessionStorage 中，绝不写入 localStorage 或服务器。
  const [apiKey, setApiKey] = useState(() => {
    if (typeof window !== "undefined" && window.sessionStorage) {
      return window.sessionStorage.getItem("cyrene_chat_api_key") ?? "";
    }
    return "";
  });

  const handleApiKeyChange = (val: string) => {
    setApiKey(val);
    if (typeof window !== "undefined" && window.sessionStorage) {
      window.sessionStorage.setItem("cyrene_chat_api_key", val);
    }
  };

  const [messages, setMessages] = useState<Array<{ role: "user" | "assistant" | "system"; content: string }>>([
    { role: "system", content: "You are a helpful AI assistant." },
  ]);
  const [inputMessage, setInputMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoadingRoute(true);
    void api.getActiveRoute().then(
      (res) => {
        if (!active) return;
        setActiveRoute(res);
        setRouteError(null);
        setLoadingRoute(false);
      },
      (err) => {
        if (!active) return;
        setActiveRoute(null);
        setRouteError(errorMessage(err));
        setLoadingRoute(false);
      },
    );
    return () => {
      active = false;
    };
  }, [api]);

  const sendMessage = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const prompt = inputMessage.trim();
    if (!prompt || sending || !activeRoute) return;
    if (!apiKey.trim()) {
      setChatError("Please enter your Exchange API key to send messages.");
      return;
    }

    const updatedMessages = [...messages, { role: "user" as const, content: prompt }];
    setMessages(updatedMessages);
    setInputMessage("");
    setSending(true);
    setChatError(null);

    const assistantIndex = updatedMessages.length;
    setMessages((prev) => [...prev, { role: "assistant", content: "" }]);

    try {
      const response = await fetch("/api/proxy/exchange-gateway/v1/chat/completions", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${apiKey.trim()}`,
          Accept: "text/event-stream, application/json",
        },
        body: JSON.stringify({
          model: activeRoute.modelId,
          messages: updatedMessages,
          stream: true,
        }),
      });

      if (!response.ok) {
        let errDetail = `HTTP ${response.status}`;
        try {
          const errJson = (await response.json()) as Record<string, unknown>;
          if (errJson && typeof errJson["detail"] === "string") {
            errDetail = errJson["detail"];
          }
        } catch {
          // ignore parse error
          // 中文：忽略解析错误。
        }
        throw new Error(errDetail);
      }

      if (response.body) {
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let accumulated = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? "";

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed || trimmed.startsWith(":")) continue;
            if (trimmed.startsWith("data:")) {
              const dataStr = trimmed.slice(5).trim();
              if (dataStr === "[DONE]") {
                break;
              }
              try {
                const parsed = JSON.parse(dataStr) as {
                  choices?: Array<{ delta?: { content?: string }; message?: { content?: string } }>;
                };
                const delta =
                  parsed.choices?.[0]?.delta?.content ??
                  parsed.choices?.[0]?.message?.content ??
                  "";
                accumulated += delta;
                setMessages((prev) => {
                  const copy = [...prev];
                  copy[assistantIndex] = { role: "assistant", content: accumulated };
                  return copy;
                });
              } catch {
                // Ignore parse errors on stream chunks
                // 中文：忽略 SSE 数据块的解析错误。
              }
            }
          }
        }
      } else {
        const data = (await response.json()) as { choices?: Array<{ message?: { content?: string } }> };
        const content = data.choices?.[0]?.message?.content ?? "";
        setMessages((prev) => {
          const copy = [...prev];
          copy[assistantIndex] = { role: "assistant", content };
          return copy;
        });
      }
    } catch (err) {
      setChatError(errorMessage(err));
      setMessages((prev) => {
        const copy = [...prev];
        if (copy[assistantIndex] && !copy[assistantIndex]?.content) {
          copy.splice(assistantIndex, 1);
        }
        return copy;
      });
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Exchange / Interactive Chat"
        title="Test models with active route."
        description="Interact directly with the model bound to this session through Exchange Gateway proxy."
      />

      {loadingRoute ? (
        <StateBlock kind="loading" title="Loading active route" detail="Checking session active route configuration." />
      ) : routeError || !activeRoute ? (
        <Panel title="No active route selected">
          <StateBlock
            kind="empty"
            title="Active route required"
            detail={routeError ?? "No gateway route has been activated for this session yet. Go to Gateway to select and activate a route."}
            action={
              <Button tone="primary" onClick={() => pushRoute("gateway")}>
                Go to Gateway
              </Button>
            }
          />
        </Panel>
      ) : (
        <>
          <Panel
            title={`Active route: ${activeRoute.modelId}`}
            meta={<StatusPill value="CONNECTED" />}
          >
            <dl className="detail-grid">
              <Detail label="Model ID" value={activeRoute.modelId} mono />
              <Detail label="Base URL" value={activeRoute.baseUrl} mono />
              <Detail label="Endpoint ID" value={activeRoute.gatewayEndpointId} mono />
              {activeRoute.apiKeyHint ? <Detail label="Key hint" value={activeRoute.apiKeyHint} /> : null}
            </dl>

            <div style={{ marginTop: "16px" }}>
              <Field
                label="Exchange API Key"
                hint="Key is held strictly in browser sessionStorage and sent via Authorization: Bearer."
              >
                <input
                  type="password"
                  value={apiKey}
                  onChange={(e) => handleApiKeyChange(e.target.value)}
                  placeholder="cyk_live_..."
                  className="input-mono"
                />
              </Field>
            </div>
          </Panel>

          <Panel
            title="Chat conversation"
            meta={
              <Button
                onClick={() =>
                  setMessages([{ role: "system", content: "You are a helpful AI assistant." }])
                }
              >
                Clear history
              </Button>
            }
          >
            {chatError ? (
              <div className="callout callout--red" style={{ marginBottom: "12px" }}>
                <span className="callout__mark" aria-hidden="true">!</span>
                <p><strong>Error:</strong> {chatError}</p>
              </div>
            ) : null}

            <div
              style={{
                display: "grid",
                gap: "12px",
                maxHeight: "450px",
                overflowY: "auto",
                padding: "12px",
                background: "var(--ink-soft)",
                borderRadius: "6px",
                border: "1px solid var(--line)",
                marginBottom: "16px",
              }}
            >
              {messages.filter((m) => m.role !== "system").length === 0 ? (
                <div style={{ color: "var(--muted)", textAlign: "center", padding: "24px 0" }}>
                  Start conversation with <code>{activeRoute.modelId}</code>
                </div>
              ) : (
                messages
                  .filter((m) => m.role !== "system")
                  .map((msg, idx) => (
                    <div
                      key={idx}
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        alignItems: msg.role === "user" ? "flex-end" : "flex-start",
                      }}
                    >
                      <span className="mono-label" style={{ marginBottom: "4px" }}>
                        {msg.role === "user" ? "YOU" : activeRoute.modelId}
                      </span>
                      <div
                        style={{
                          maxWidth: "85%",
                          padding: "10px 14px",
                          borderRadius: "8px",
                          background:
                            msg.role === "user"
                              ? "rgba(201, 242, 123, 0.12)"
                              : "rgba(17, 29, 34, 0.9)",
                          border: `1px solid ${
                            msg.role === "user" ? "var(--lime)" : "var(--line)"
                          }`,
                          whiteSpace: "pre-wrap",
                          fontSize: "13px",
                          lineHeight: "1.5",
                        }}
                      >
                        {msg.content || (sending && idx === messages.length - 1 ? "..." : "")}
                      </div>
                    </div>
                  ))
              )}
            </div>

            <form onSubmit={sendMessage} style={{ display: "flex", gap: "8px" }}>
              <input
                style={{ flex: 1, padding: "10px 14px", borderRadius: "6px" }}
                value={inputMessage}
                onChange={(e) => setInputMessage(e.target.value)}
                placeholder="Type a message..."
                disabled={sending || !apiKey.trim()}
              />
              <Button tone="primary" type="submit" disabled={sending || !inputMessage.trim() || !apiKey.trim()}>
                {sending ? "Sending..." : "Send"}
              </Button>
            </form>
          </Panel>
        </>
      )}
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
