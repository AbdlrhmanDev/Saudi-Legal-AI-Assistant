const PILLS: Record<string, [string, string, string]> = {
  active: ['سارية', '#E2EFE7', '#17543D'],
  scheduled: ['نفاذ مستقبلي', '#E4EAF4', '#28467A'],
  superseded: ['مُستبدَل', '#ECEBE6', '#4A4F4C'],
  repealed: ['ملغى', '#ECEBE6', '#4A4F4C'],
  rolled_back: ['أُلغي تفعيله', '#ECEBE6', '#4A4F4C'],
  needs_review: ['بحاجة لمراجعة', '#FBE6D9', '#8A3A12'],
  pending: ['قيد المراجعة', '#FBF0D9', '#7A4F00'],
  candidate: ['بانتظار النص الرسمي', '#FBF0D9', '#7A4F00'],
  approved: ['معتمد', '#E2EFE7', '#17543D'],
  rejected: ['مرفوض', '#ECEBE6', '#4A4F4C'],
  grounded: ['مستندة إلى مصادر', '#E2EFE7', '#17543D'],
  partial: ['إجابة جزئية', '#FBF0D9', '#7A4F00'],
  insufficient_evidence: ['أدلة غير كافية', '#ECEBE6', '#4A4F4C'],
  error: ['خطأ', '#F8E3E1', '#8E2626'],
  succeeded: ['ناجحة', '#E2EFE7', '#17543D'],
  duplicate: ['مكررة', '#ECEBE6', '#4A4F4C'],
  failed: ['فشلت', '#F8E3E1', '#8E2626'],
  queued: ['في الانتظار', '#FBF0D9', '#7A4F00'],
  running: ['قيد التنفيذ', '#FBF0D9', '#7A4F00'],
  quarantined: ['محجوز آليًا', '#F8E3E1', '#8E2626'],
  excluded: ['مستبعد', '#ECEBE6', '#4A4F4C'],
  no_op: ['لا جديد', '#ECEBE6', '#4A4F4C'],
  run_partial: ['نجحت جزئيًا', '#FBF0D9', '#7A4F00'],
  healthy: ['محدّثة', '#E2EFE7', '#17543D'],
  delayed: ['متأخرة', '#FBF0D9', '#7A4F00'],
  degraded: ['غير مؤكدة', '#F8E3E1', '#8E2626'],
  out_of_scope: ['خارج النطاق', '#ECEBE6', '#4A4F4C'],
  draft_status: ['مشروع غير نافذ', '#FBF0D9', '#7A4F00'],
  // official source registry: reachability and integration
  unknown: ['لم يُفحص', '#ECEBE6', '#4A4F4C'],
  available: ['متاح', '#E2EFE7', '#17543D'],
  unavailable: ['غير متاح', '#F8E3E1', '#8E2626'],
  blocked: ['محجوب عن الأدوات الآلية', '#F8E3E1', '#8E2626'],
  robots_disallowed: ['يمنعه robots.txt', '#F8E3E1', '#8E2626'],
  integrated: ['مدمج', '#E2EFE7', '#17543D'],
  discovery_only: ['رصد تغييرات فقط', '#FBF0D9', '#7A4F00'],
  not_integrated: ['غير مدمج', '#ECEBE6', '#4A4F4C'],
  consolidated: ['مدمج مع مصدر آخر', '#E4EAF4', '#28467A'],
  changed: ['تغيّر', '#E4EAF4', '#28467A'],
  no_change: ['لا تغيير', '#ECEBE6', '#4A4F4C'],
  skipped: ['تخطّي', '#ECEBE6', '#4A4F4C'],
}
const FALLBACK: [string, string, string] = ['—', '#ECEBE6', '#4A4F4C']

export const pillOf = (key: string) => {
  const [label, bg, fg] = PILLS[key] ?? [key, FALLBACK[1], FALLBACK[2]]
  return { label, bg, fg }
}

export const DOT: Record<string, string> = {
  active: '#2E7D5B',
  scheduled: '#3C64A8',
  superseded: '#A8A69E',
  needs_review: '#C0602A',
}

export const ICON = {
  chat: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z',
  book: 'M4 19.5A2.5 2.5 0 0 1 6.5 17H20M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z',
  refresh: 'M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5',
  pulse: 'M22 12h-4l-3 9L9 3l-3 9H2',
  doc: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M16 13H8M16 17H8',
  logout: 'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9',
  login: 'M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4M10 17l5-5-5-5M15 12H3',
  calendar: 'M8 2v4M16 2v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z',
  openBook: 'M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2zM22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z',
  chevron: 'M15 18l-6-6 6-6',
  info: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-4M12 8h.01',
  shield: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z',
  spark: 'M12 3l1.9 5.8L20 10l-6.1 1.2L12 17l-1.9-5.8L4 10l6.1-1.2z',
  send: 'M19 12H5M12 19l-7-7 7-7',
  close: 'M18 6 6 18M6 6l12 12',
  external: 'M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14 21 3',
  upload: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12',
  list: 'M4 6h16M4 12h16M4 18h10',
  clock: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2',
  warn: 'M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01',
  lock: 'M5 11h14a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2zM7 11V7a5 5 0 0 1 10 0v4',
  check: 'M20 6 9 17l-5-5',
}

const dateFmt = new Intl.DateTimeFormat('ar-SA-u-ca-gregory-nu-latn', { day: 'numeric', month: 'long', year: 'numeric' })
const timeFmt = new Intl.DateTimeFormat('ar-SA-u-ca-gregory-nu-latn', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' })

export function fmtDate(d: string | null | undefined): string {
  if (!d) return '—'
  const day = d.slice(0, 10)
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return d
  return dateFmt.format(new Date(day + 'T00:00:00'))
}
export const fmtDateTime = (d: string | null | undefined) => (d ? timeFmt.format(new Date(d)) : '—')
// Sync times are stated in Saudi time whatever the viewer's own time zone is.
const riyadhFmt = new Intl.DateTimeFormat('ar-SA-u-ca-gregory-nu-latn', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Riyadh' })
export const fmtRiyadh = (d: string | null | undefined) => (d ? `${riyadhFmt.format(new Date(d))} (بتوقيت الرياض)` : '—')
export const shortId = (id: string) => id.slice(0, 8)
export const todayIso = () => {
  const now = new Date()
  return new Date(now.getTime() - now.getTimezoneOffset() * 60_000).toISOString().slice(0, 10)
}

export const DOMAIN_AR: Record<string, string> = {
  labor: 'العمل', commercial: 'التجارة والشركات', data_protection: 'حماية البيانات', other: 'أخرى', real_estate: 'العقار',
  tax_customs: 'الزكاة والضريبة والجمارك', banking_finance: 'البنوك والتمويل', capital_markets: 'السوق المالية', insurance: 'التأمين',
  investment: 'الاستثمار', social_insurance: 'التأمينات الاجتماعية', health: 'الصحة', food_drug: 'الغذاء والدواء', transport: 'النقل',
  municipal: 'البلديات والإسكان', cybersecurity: 'الأمن السيبراني', digital_telecom: 'الاتصالات والحكومة الرقمية',
  intellectual_property: 'الملكية الفكرية', judicial: 'القضاء والأحوال الشخصية', administrative: 'الإدارة العامة', criminal: 'الجزائي',
  consumer: 'حماية المستهلك', competition: 'المنافسة',
}
export const TYPE_AR: Record<string, string> = { law: 'نظام', implementing_regulation: 'لائحة تنفيذية', regulation: 'لائحة', guidance: 'دليل إرشادي', notice: 'تعميم', draft: 'مشروع', other: 'وثيقة' }
export const CLASS_AR: Record<string, string> = { new_law: 'نظام جديد', amendment: 'تعديل نظام', repeal: 'إلغاء نظام', regulation: 'لائحة أو قواعد', royal_decree: 'أمر أو مرسوم ملكي', decision: 'قرار', circular: 'تعميم', notice: 'إشعار أو إعلان', draft: 'مشروع أو مسودة', irrelevant: 'غير ذي صلة' }
export const REASON_AR: Record<string, string> = {
  NO_OFFICIAL_TEXT: 'لا يتوفر نص رسمي قابل للجلب الآلي',
  SOURCE_UNAVAILABLE: 'تعذّر جلب النص الرسمي؛ ستُعاد المحاولة',
  NOT_OFFICIAL_SOURCE: 'المصدر غير رسمي أو خارج القائمة المعتمدة',
  PROMPT_INJECTION_DETECTED: 'يحتوي المستند على تعليمات موجهة لنظام ذكاء اصطناعي',
  ARTICLE_STRUCTURE_INVALID: 'تعذّر استخراج مواد مرقّمة من النص',
  CONFLICTING_ARTICLE_NUMBERS: 'أرقام مواد متكررة أو متعارضة',
  TARGET_UNRESOLVED: 'تعذّر تحديد النظام المعدَّل أو الملغى',
  AMBIGUOUS_AMENDMENT_MAPPING: 'ربط المواد المعدّلة غير مؤكد (وثيقة تعديل جزئية)',
  EFFECTIVE_DATE_MISSING: 'لا يوجد تاريخ نفاذ موثّق في النص',
  CONFLICTING_EFFECTIVE_DATES: 'تواريخ نفاذ متعارضة في المصادر الرسمية',
  HIJRI_DATE_UNVERIFIED: 'تاريخ النفاذ هجري فقط ولم يُحوَّل آليًا',
  PUBLICATION_DATE_MISSING: 'النفاذ مرتبط بتاريخ نشر غير معروف',
  CONFLICTING_EFFECTIVE_VERSION: 'يتعارض مع إصدار معتمد آخر',
  AGENT_RULES_DISAGREE: 'اختلف تصنيف الوكيل عن القواعد الحتمية',
  EMBEDDING_BUDGET_EXCEEDED: 'تجاوز حد الفهرسة لهذه الليلة؛ ستُعاد المحاولة',
  INDEX_HEALTH_FAILED: 'فشل فحص سلامة الفهرس؛ بقي الفهرس السابق',
  PUBLISH_FAILED: 'تعذّر النشر؛ ستُعاد المحاولة',
  NOT_A_LEGAL_CHANGE: 'ليس تغييرًا نظاميًا (مشروع أو خبر أو إشعار)',
  STATUS_NOT_IN_FORCE: 'حالة النظام لدى هيئة الخبراء ليست «ساري»',
}
export const WARNING_AR: Record<string, string> = { possible_prompt_injection: 'يحتوي النص على عبارات تبدو تعليمات موجهة للمساعد', possible_partial_amendment_document: 'قد تكون الوثيقة مقتصرة على المواد المعدّلة وليست نصًا موحدًا', no_articles_detected: 'لم تُكتشف مواد مرقّمة في الوثيقة' }
export const GAP_AR: Record<string, string> = { source_url: 'رابط المصدر', source_url_not_allowlisted: 'المصدر خارج القائمة المعتمدة', issuing_authority: 'الجهة المصدرة', published_at: 'تاريخ النشر', effective_from: 'تاريخ النفاذ' }

export type Flash = (text: string, kind?: 'err') => void
