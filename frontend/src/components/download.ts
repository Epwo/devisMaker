import { api } from "../api";
import type { Job } from "../types";

export async function downloadQuote(job: Job) {
  if (!job.ext) throw new Error("Plan non analysé");
  const blob = await api.pdf(job.ext, job.quantity, job.client, job.overrides);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `devis_${job.file.name.replace(/\.[^.]+$/, "")}.pdf`;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
