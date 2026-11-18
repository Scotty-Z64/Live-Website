export type Issue = {
  id: string; code: string; severity: "BLOCKER" | "WARNING"; message: string; remediation: string;
  action: null | { type: string; label: string; doc_category?: string; addendum_id?: string; document_id?: string };
};

export type VaultDoc = {
  id: string; doc_category: string | null; original_filename: string; sha256_hash: string;
  ocr_confidence_score: number | null; extracted_json: Record<string, unknown>; issue_date: string | null; expiry_date: string | null;
  validation_status: string | null; validation_errors: { code: string; message: string; remediation: string; severity: string }[];
  processing_state: string; av_status: string; uploaded_at: string; url: string | null; is_current: boolean;
};

export type TenderBrief = {
  id: string; source_portal: string; source_tier: string | null; tender_reference: string; title: string | null;
  issuing_entity: string | null; closing_timestamp: string | null; submission_mode: string | null; has_compulsory_briefing: boolean;
  briefing_timestamp: string | null; preflight_done: boolean; unacknowledged_addenda: number;
  bid?: { id: string; status: string; readiness_score_pct: number } | null;
};

export type Addendum = {
  id: string; detected_at: string; url: string; filename: string; old_sha256: string | null; new_sha256: string; kind: string;
  acknowledged: boolean; acknowledged_by?: string; acknowledged_at?: string;
  diff: { changed_fields: Record<string, unknown>; lines_added: number; lines_removed: number; sample_added: string[]; sample_removed: string[] };
};

export type Citation = { file: string; page: number | null; snippet: string };

export type Tender = TenderBrief & {
  description: string | null; portal_url: string | null; physical_tender_box_address: string | null; preference_system: string | null;
  functionality_threshold_pct: number | null; briefing_venue_or_link: string | null; briefing_registration_url: string | null;
  mandatory_schedules: { name: string; citation: Citation }[];
  boq_line_items: { line_no: string; description: string; unit: string; quantity: number }[];
  addenda_history: Addendum[];
  preflight: {
    zones?: Record<string, { label: string; spans: { file: string; page_from: number; page_to: number }[]; chars: number }>;
    fields?: Record<string, any>; citations?: Record<string, Citation>; llm_used?: boolean;
  };
};

export type PricingLine = {
  line_no: string; description: string; unit: string; quantity: number; unit_cost: number | null; unit_price: number | null;
  line_total: number | null; local_content_pct: number | null; manual?: boolean;
  match: null | { catalog_item_id: string; sku_code: string; item_name: string; unit_price_excl_vat: number; similarity: number | null };
  candidates: { catalog_item_id: string; sku_code: string; item_name: string; unit_price_excl_vat: number; similarity: number }[];
};

export type Bid = {
  id: string; tender_id: string; preflight_complete: boolean; briefing_gate_status: string; readiness_score_pct: number | null;
  blocking_issues: Issue[]; status: string; letter_of_authority_url: string | null; binder_zip_url: string | null;
  briefing_representative: Record<string, any>;
  pricing_summary: { margin_pct?: number; lines?: PricingLine[]; subtotal_excl_vat?: number; vat?: number; total_incl_vat?: number;
    unpriced_lines?: string[]; local_content_weighted_pct?: number | null; overrides?: Record<string, any> };
  sbd_outputs: Record<string, { url: string; method: string; filled: string[]; missing_values: string[]; source: string }>;
  submission_receipt: Record<string, any>;
};

export const CATEGORY_LABELS: Record<string, string> = {
  CIPC_COR14_3: "CIPC Disclosure (CoR14.3)", CSD_REPORT: "CSD Summary Report", SARS_TCS_PIN: "SARS Tax Compliance PIN",
  BBBEE_AFFIDAVIT: "B-BBEE Sworn Affidavit", BBBEE_SANAS_CERT: "B-BBEE SANAS Certificate", COIDA_LOGS: "COIDA Letter of Good Standing",
  CIDB_CERT: "CIDB Registration", PSIRA_CERT: "PSIRA Registration", WIREMAN_LICENSE: "DoEL Wireman's Licence",
  BANK_CONFIRMATION: "Bank Confirmation Letter", DIRECTOR_ID: "Certified Director IDs", COMPANY_PROFILE: "Company Profile",
};
