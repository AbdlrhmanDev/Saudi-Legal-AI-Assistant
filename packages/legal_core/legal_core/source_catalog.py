"""The official Saudi source catalog: every entry of the approved source list, in code.

The catalog is the single definition of which official endpoints exist, what kind of material each
one publishes, how its connector reads it, and which hosts that connector may contact. `cli
seed-sources` mirrors it into `source_registry`; operators can enable/disable rows but cannot add
hosts, so the fetch allowlist stays a code-reviewed list.

Connector types:
- rss:                official RSS feed; items link to full official text (Umm Al-Qura).
- html_listing:       a server-rendered listing page; links matching `link_patterns` become candidates.
- moj_judgments_api:  the Ministry of Justice public judgments API (judicial material, stored apart).
- page_watch:         the page renders its list with JavaScript, blocks automated clients, or is a
                      supporting/directory page: only a change of its visible text is detected and
                      reported. Nothing from it ever becomes legal evidence.
- none:               no permitted automated access path; registered and probed for availability only.

Access notes record what was observed when the endpoints were verified (2026-10-22). They are
refreshed in the database by every probe; the notes here explain the connector choice.
"""

import re
from dataclasses import dataclass, field

CATEGORIES = {
    "A": "المصادر النظامية الأساسية",
    "B": "التجارة والمالية والعمل والاستثمار والضرائب",
    "C": "العقار والبلديات والصحة والنقل",
    "D": "التنظيمات الرقمية والخصوصية والأمن السيبراني والملكية الفكرية",
    "E": "الأحكام القضائية والمبادئ",
    "F": "مصادر داعمة لرصد التحديثات",
}
AUTHORITY_TIERS = ("primary", "sectoral", "judicial", "supporting")
DOCUMENT_KINDS = ("legislation", "gazette", "judicial", "draft", "news", "directory", "archive")
CONNECTOR_TYPES = ("rss", "html_listing", "moj_judgments_api", "page_watch", "none")

# Links to a consolidated law page on the Bureau of Experts portal. Sector sites link here, and the
# Bureau's page (not the sector page) is the authoritative text, so these links are attributed to
# the Bureau of Experts source.
BOE_LAW_PATTERN = r"^https://laws\.boe\.gov\.sa/boelaws/laws/lawdetails/[0-9a-f-]{36}/1$"
# Document files, optionally followed by a cache/version query (".pdf?MOD=AJPERES").
PDF_PATTERN = r"\.(pdf|docx?)(?:\?.*)?$"


@dataclass(frozen=True)
class CatalogSource:
    key: str
    numbers: tuple[int, ...]  # entry numbers in the approved source list
    name: str  # registry name (unique, stable)
    name_ar: str
    authority: str
    url: str  # URL exactly as listed
    endpoint: str  # URL the connector reads (redirect targets verified)
    category: str
    tier: str
    document_kind: str
    connector: str
    hosts: tuple[str, ...]
    default_domain: str = "other"
    enabled: bool = True
    consolidated_into: str | None = None
    link_patterns: tuple[str, ...] = ()
    max_items: int = 15
    access_notes: str = ""
    config: dict = field(default_factory=dict)


def _s(**kwargs) -> CatalogSource:
    return CatalogSource(**kwargs)


SOURCES: tuple[CatalogSource, ...] = (
    # --- A. Primary legal sources ---------------------------------------------------------------
    _s(key="boe", numbers=(1,), name="Bureau of Experts - Saudi Laws Portal", name_ar="هيئة الخبراء بمجلس الوزراء",
       authority="Bureau of Experts at the Council of Ministers", url="https://www.boe.gov.sa/",
       endpoint="https://laws.boe.gov.sa/BoeLaws/Laws/Folders/1", category="A", tier="primary",
       document_kind="legislation", connector="html_listing", hosts=("laws.boe.gov.sa", "www.boe.gov.sa"),
       link_patterns=(BOE_LAW_PATTERN,), max_items=10,
       access_notes="Folders/1 lists ~517 consolidated laws (LawDetails pages, server-rendered). The bare host "
                    "laws.boe.gov.sa redirects to plain http://www.boe.gov.sa and is never followed; only HTTPS "
                    "LawDetails pages are fetched. The server omits its intermediate certificate (bundled)."),
    _s(key="uqn_rss", numbers=(2,), name="Umm Al-Qura RSS - Decisions & Regulations", name_ar="جريدة أم القرى",
       authority="Umm Al-Qura (Official Gazette)", url="https://www.uqn.gov.sa/",
       endpoint="https://www.uqn.gov.sa/rssFeed/21", category="A", tier="primary", document_kind="gazette",
       connector="rss", hosts=("www.uqn.gov.sa", "uqn.gov.sa"), max_items=50,
       access_notes="rssFeed/21 (decisions and regulations) returns full decision text; item pages are parsed by "
                    "the Umm Al-Qura HTML parser. The Gazette is used to verify publication and effective dates."),
    _s(key="ncar", numbers=(3,), name="National Center for Archives and Records", name_ar="المركز الوطني للوثائق والمحفوظات",
       authority="National Center for Archives and Records", url="https://ncar.gov.sa/", endpoint="https://ncar.gov.sa/",
       category="A", tier="primary", document_kind="archive", connector="page_watch", hosts=("ncar.gov.sa",),
       access_notes="The site is a JavaScript application with no server-rendered document links; change detection only."),
    _s(key="moj_laws", numbers=(4,), name="Ministry of Justice - Legal Portal", name_ar="البوابة القانونية - وزارة العدل",
       authority="Ministry of Justice", url="https://laws.moj.gov.sa/", endpoint="https://laws.moj.gov.sa/",
       category="A", tier="primary", document_kind="legislation", connector="page_watch",
       hosts=("laws.moj.gov.sa", "laws-gateway.moj.gov.sa"),
       access_notes="Nuxt single-page application. Its statute search endpoint (laws-gateway.moj.gov.sa "
                    "/statute/section-search) rejected automated requests with a web-application-firewall page, so "
                    "legislation is not harvested here; the same laws are taken from the Bureau of Experts. Judicial "
                    "decisions from this portal are integrated through entry 31."),
    _s(key="bog", numbers=(5,), name="Board of Grievances", name_ar="ديوان المظالم",
       authority="Board of Grievances", url="https://www.bog.gov.sa/", endpoint="https://www.bog.gov.sa/Pages/default.aspx",
       category="A", tier="judicial", document_kind="judicial", connector="html_listing", hosts=("www.bog.gov.sa",),
       default_domain="administrative",
       link_patterns=(r"/knowledge-center/(judicialblogs|principlesblogs)/[^/]+/pages/default\.aspx$",), max_items=10,
       access_notes="Knowledge-center pages list published collections of administrative judgments and principles "
                    "of the High Administrative Court; stored as judicial material, never as legislation."),
    _s(key="istitlaa", numbers=(6,), name="Istitlaa Public Consultation Platform", name_ar="منصة استطلاع",
       authority="National Competitiveness Center (Istitlaa)", url="https://istitlaa.ncc.gov.sa/",
       endpoint="https://istitlaa.scbc.gov.sa/ar/Pages/Projects.aspx", category="A", tier="primary",
       document_kind="draft", connector="page_watch", hosts=("istitlaa.ncc.gov.sa", "istitlaa.scbc.gov.sa"),
       access_notes="istitlaa.ncc.gov.sa redirects to istitlaa.scbc.gov.sa. The project list is rendered by "
                    "JavaScript and the sitemap lists static sections only. Everything here is a DRAFT and is "
                    "never published as enforceable law."),
    _s(key="my_gov", numbers=(7,), name="Saudi National Unified Platform", name_ar="المنصة الوطنية الموحدة",
       authority="Digital Government Authority (my.gov.sa)", url="https://my.gov.sa/", endpoint="https://my.gov.sa/ar",
       category="A", tier="supporting", document_kind="directory", connector="page_watch", hosts=("my.gov.sa",),
       access_notes="Returns HTTP 403 to automated clients (web-application firewall). Not bypassed; reported "
                    "as blocked."),
    # --- B. Commercial, financial, labor, investment and tax ------------------------------------
    _s(key="mc", numbers=(8,), name="Ministry of Commerce - Regulations", name_ar="وزارة التجارة - الأنظمة",
       authority="Ministry of Commerce", url="https://mc.gov.sa/ar/Regulations/pages/default.aspx",
       endpoint="https://mc.gov.sa/ar/Regulations/pages/default.aspx", category="B", tier="sectoral",
       document_kind="legislation", connector="page_watch", hosts=("mc.gov.sa",), default_domain="commercial",
       access_notes="SharePoint page whose regulation list is loaded by script: the 2026-10-22 probe found no "
                    "server-rendered regulation links, so changes are detected but documents are not harvested here. "
                    "Commerce laws are harvested from the Bureau of Experts (entry 1)."),
    _s(key="hrsd", numbers=(9,), name="HRSD - Labor Law", name_ar="وزارة الموارد البشرية والتنمية الاجتماعية",
       authority="Ministry of Human Resources and Social Development",
       url="https://www.hrsd.gov.sa/knowledge-centre/decisions-and-regulations/regulation-and-procedures",
       endpoint="https://www.hrsd.gov.sa/knowledge-centre/decisions-and-regulations/regulation-and-procedures",
       category="B", tier="sectoral", document_kind="legislation", connector="page_watch",
       hosts=("www.hrsd.gov.sa", "hrsd.gov.sa"), default_domain="labor",
       access_notes="The regulations page links to sub-sections, not to documents (2026-10-22 probe), so changes are "
                    "detected only. The Labor Law and its regulations are harvested from the Bureau of Experts (entry 1)."),
    _s(key="gosi", numbers=(10,), name="General Organization for Social Insurance", name_ar="المؤسسة العامة للتأمينات الاجتماعية",
       authority="General Organization for Social Insurance", url="https://beta.gosi.gov.sa/laws-regualtion",
       endpoint="https://beta.gosi.gov.sa/laws-regualtion", category="B", tier="sectoral", document_kind="legislation",
       connector="page_watch", hosts=("beta.gosi.gov.sa",), default_domain="social_insurance",
       access_notes="The regulations list is rendered client-side (no document links in the HTML); change detection only."),
    _s(key="misa", numbers=(11,), name="Ministry of Investment - Laws", name_ar="وزارة الاستثمار - الأنظمة",
       authority="Ministry of Investment", url="https://misa.gov.sa/ar/activities/laws/",
       endpoint="https://misa.gov.sa/ar/activities/laws/", category="B", tier="sectoral", document_kind="legislation",
       connector="html_listing", hosts=("misa.gov.sa",), default_domain="investment", link_patterns=(BOE_LAW_PATTERN,),
       max_items=10,
       access_notes="Lists ~170 laws as links to Bureau of Experts pages; those are attributed to entry 1."),
    _s(key="zatca", numbers=(12,), name="Zakat, Tax and Customs Authority", name_ar="هيئة الزكاة والضريبة والجمارك",
       authority="Zakat, Tax and Customs Authority", url="https://www.zatca.gov.sa/ar/RulesRegulations/Pages/systems.aspx",
       endpoint="https://www.zatca.gov.sa/ar/RulesRegulations/Pages/systems.aspx", category="B", tier="sectoral",
       document_kind="legislation", connector="page_watch", hosts=("www.zatca.gov.sa", "zatca.gov.sa"),
       default_domain="tax_customs",
       access_notes="SharePoint list rendered by script; no server-rendered document links. Change detection only."),
    _s(key="sama", numbers=(13,), name="Saudi Central Bank Rulebook", name_ar="البنك المركزي السعودي - دليل الأنظمة",
       authority="Saudi Central Bank", url="https://rulebook.sama.gov.sa/ar", endpoint="https://rulebook.sama.gov.sa/ar",
       category="B", tier="sectoral", document_kind="legislation", connector="none", hosts=("rulebook.sama.gov.sa",),
       default_domain="banking_finance", enabled=False,
       access_notes="robots.txt disallows all automated agents except Google's crawlers (User-agent: * Disallow: /), "
                    "and the site answered HTTP 503. Not crawled. Content must be obtained under permission."),
    _s(key="cma", numbers=(14,), name="Capital Market Authority", name_ar="هيئة السوق المالية",
       authority="Capital Market Authority", url="https://cma.gov.sa/RulesRegulations/Pages/default.aspx",
       endpoint="https://cma.gov.sa/RulesRegulations/Pages/default.aspx", category="B", tier="sectoral",
       document_kind="legislation", connector="page_watch", hosts=("cma.gov.sa", "www.cma.gov.sa"),
       default_domain="capital_markets",
       access_notes="Rules list is loaded by script; change detection only."),
    _s(key="ia", numbers=(15,), name="Insurance Authority", name_ar="هيئة التأمين", authority="Insurance Authority",
       url="https://www.ia.gov.sa/ar/Regulations", endpoint="https://www.ia.gov.sa/ar/Regulations", category="B",
       tier="sectoral", document_kind="legislation", connector="html_listing", hosts=("www.ia.gov.sa", "ia.gov.sa"),
       default_domain="insurance", link_patterns=(r"^https://www\.ia\.gov\.sa/regulations/.+" + PDF_PATTERN,),
       access_notes="Server-rendered list of regulation PDFs (file names carry the Arabic titles)."),
    _s(key="mof", numbers=(16,), name="Ministry of Finance - Regulations", name_ar="وزارة المالية - الأنظمة والتعليمات",
       authority="Ministry of Finance", url="https://www.mof.gov.sa/docslibrary/RegulationsInstructions/Pages/default.aspx",
       endpoint="https://www.mof.gov.sa/docslibrary/RegulationsInstructions/Pages/default.aspx", category="B",
       tier="sectoral", document_kind="legislation", connector="html_listing", hosts=("www.mof.gov.sa", "mof.gov.sa"),
       default_domain="administrative",
       link_patterns=(BOE_LAW_PATTERN, r"^https://www\.mof\.gov\.sa/docslibrary/regulationsinstructions/documents/.+"
                      + PDF_PATTERN),
       access_notes="Document library folders; budget PDFs elsewhere on the page are excluded by the link rules."),
    _s(key="gac", numbers=(17,), name="General Authority for Competition", name_ar="الهيئة العامة للمنافسة",
       authority="General Authority for Competition", url="https://www.gac.gov.sa/", endpoint="https://www.gac.gov.sa/",
       category="B", tier="sectoral", document_kind="legislation", connector="page_watch", hosts=("www.gac.gov.sa",),
       default_domain="competition", access_notes="Home page only in the approved list; change detection only."),
    # --- C. Real estate, municipal, health and transport ----------------------------------------
    _s(key="rega", numbers=(18,), name="Real Estate General Authority", name_ar="الهيئة العامة للعقار",
       authority="Real Estate General Authority",
       url="https://rega.gov.sa/الأنظمة-والقرارات/الأنظمة-واللوائح-والأدلة/",
       endpoint="https://rega.gov.sa/%D8%A7%D9%84%D8%A3%D9%86%D8%B8%D9%85%D8%A9-%D9%88%D8%A7%D9%84%D9%82%D8%B1%D8%A7%D8%B1%D8%A7%D8%AA/%D8%A7%D9%84%D8%A3%D9%86%D8%B8%D9%85%D8%A9-%D9%88%D8%A7%D9%84%D9%84%D9%88%D8%A7%D8%A6%D8%AD-%D9%88%D8%A7%D9%84%D8%A3%D8%AF%D9%84%D8%A9/",
       category="C", tier="sectoral", document_kind="legislation", connector="page_watch", hosts=("rega.gov.sa",),
       default_domain="real_estate",
       access_notes="The regulations list is loaded from a disallowed data path (/JsonFile/ in robots.txt); "
                    "not fetched. Change detection of the public page only."),
    _s(key="momah", numbers=(19,), name="Ministry of Municipalities and Housing", name_ar="وزارة البلديات والإسكان",
       authority="Ministry of Municipalities and Housing", url="https://momah.gov.sa/ar/regulations",
       endpoint="https://momah.gov.sa/ar/regulations", category="C", tier="sectoral", document_kind="legislation",
       connector="html_listing", hosts=("momah.gov.sa",), default_domain="municipal",
       link_patterns=(r"^https://momah\.gov\.sa/sites/default/files/.+" + PDF_PATTERN,),
       access_notes="Server-rendered list of regulation and requirement PDFs."),
    _s(key="moh", numbers=(20,), name="Ministry of Health - Rules", name_ar="وزارة الصحة - الأنظمة واللوائح",
       authority="Ministry of Health", url="https://www.moh.gov.sa/Ministry/Rules/Pages/default.aspx",
       endpoint="https://www.moh.gov.sa/Ministry/Rules/Pages/default.aspx", category="C", tier="sectoral",
       document_kind="legislation", connector="page_watch", hosts=("www.moh.gov.sa",), default_domain="health",
       access_notes="SharePoint page whose rules list is loaded by script (no document links in the HTML on the "
                    "2026-10-22 probe); change detection only. Health laws are harvested from the Bureau of Experts."),
    _s(key="sfda", numbers=(21,), name="Saudi Food and Drug Authority", name_ar="الهيئة العامة للغذاء والدواء",
       authority="Saudi Food and Drug Authority", url="https://www.sfda.gov.sa/ar/regulations",
       endpoint="https://www.sfda.gov.sa/ar/regulations", category="C", tier="sectoral", document_kind="legislation",
       connector="html_listing", hosts=("www.sfda.gov.sa",), default_domain="food_drug",
       link_patterns=(r"^https://www\.sfda\.gov\.sa/sites/default/files/.+" + PDF_PATTERN,),
       access_notes="Server-rendered list of regulation PDFs (first page)."),
    _s(key="scfhs", numbers=(22,), name="Saudi Commission for Health Specialties", name_ar="الهيئة السعودية للتخصصات الصحية",
       authority="Saudi Commission for Health Specialties", url="https://scfhs.org.sa/ar/ClassGuide",
       endpoint="https://scfhs.org.sa/ar/ClassGuide", category="C", tier="sectoral", document_kind="legislation",
       connector="html_listing", hosts=("scfhs.org.sa",), default_domain="health",
       link_patterns=(r"^https://scfhs\.org\.sa/sites/default/files/.+" + PDF_PATTERN,),
       access_notes="Classification guide page; document links when server-rendered."),
    _s(key="tga", numbers=(23,), name="Transport General Authority", name_ar="الهيئة العامة للنقل",
       authority="Transport General Authority", url="https://www.tga.gov.sa/", endpoint="https://tga.gov.sa/ar",
       category="C", tier="sectoral", document_kind="legislation", connector="page_watch",
       hosts=("www.tga.gov.sa", "tga.gov.sa"), default_domain="transport",
       access_notes="www.tga.gov.sa redirects to tga.gov.sa/ar (home page); change detection only."),
    # --- D. Digital, privacy, cybersecurity and IP -----------------------------------------------
    _s(key="sdaia", numbers=(24,), name="SDAIA - Regulations and Policies", name_ar="سدايا - الأنظمة والسياسات",
       authority="Saudi Data & AI Authority", url="https://sdaia.gov.sa/en/SDAIA/about/Pages/RegulationsAndPolicies.aspx",
       endpoint="https://sdaia.gov.sa/en/SDAIA/about/Pages/RegulationsAndPolicies.aspx", category="D", tier="sectoral",
       document_kind="legislation", connector="html_listing", hosts=("sdaia.gov.sa",), default_domain="data_protection",
       link_patterns=(r"^https://sdaia\.gov\.sa/(ar|en)/sdaia/about/(documents|files)/.+" + PDF_PATTERN,),
       access_notes="Server-rendered list of regulation and policy PDFs (English page; Arabic text governs)."),
    _s(key="dgp", numbers=(25,), name="SDAIA - PDPL Knowledge Center", name_ar="المنصة الوطنية لحوكمة البيانات",
       authority="Saudi Data & AI Authority", url="https://dgp.sdaia.gov.sa/wps/portal/pdp/knowledgecenter/",
       endpoint="https://dgp.sdaia.gov.sa/wps/portal/pdp/knowledgecenter/", category="D", tier="sectoral",
       document_kind="legislation", connector="html_listing", hosts=("dgp.sdaia.gov.sa",), default_domain="data_protection",
       link_patterns=(r"^https://dgp\.sdaia\.gov\.sa/wps/wcm/connect/.+" + PDF_PATTERN,),
       access_notes="Knowledge center exposes policy and regulation PDFs."),
    _s(key="nca", numbers=(26,), name="National Cybersecurity Authority", name_ar="الهيئة الوطنية للأمن السيبراني",
       authority="National Cybersecurity Authority", url="https://nca.gov.sa/ar/regulatory-documents/",
       endpoint="https://nca.gov.sa/ar/regulatory-documents/", category="D", tier="sectoral", document_kind="legislation",
       connector="page_watch", hosts=("nca.gov.sa",), default_domain="cybersecurity",
       access_notes="Regulatory documents are rendered by script (no document links in the HTML); change detection only."),
    _s(key="dga", numbers=(27,), name="Digital Government Authority", name_ar="هيئة الحكومة الرقمية",
       authority="Digital Government Authority", url="https://dga.gov.sa/", endpoint="https://dga.gov.sa/ar",
       category="D", tier="sectoral", document_kind="legislation", connector="page_watch", hosts=("dga.gov.sa",),
       default_domain="digital_telecom", access_notes="Home page only in the approved list; change detection only."),
    _s(key="cst", numbers=(28,), name="Communications, Space and Technology Commission", name_ar="هيئة الاتصالات والفضاء والتقنية",
       authority="Communications, Space and Technology Commission", url="https://www.cst.gov.sa/",
       endpoint="https://www.cst.gov.sa/", category="D", tier="sectoral", document_kind="legislation",
       connector="page_watch", hosts=("www.cst.gov.sa",), default_domain="digital_telecom",
       access_notes="Home page only in the approved list; change detection only."),
    _s(key="saip", numbers=(29,), name="Saudi Authority for Intellectual Property", name_ar="الهيئة السعودية للملكية الفكرية",
       authority="Saudi Authority for Intellectual Property",
       url="https://saip.gov.sa/ar/resources/lows-and-regulations/systems-and-regulations",
       endpoint="https://saip.gov.sa/ar/resources/lows-and-regulations/systems-and-regulations", category="D",
       tier="sectoral", document_kind="legislation", connector="page_watch", hosts=("saip.gov.sa",),
       default_domain="intellectual_property",
       access_notes="Systems list is rendered by script; change detection only."),
    _s(key="sdaia_archive", numbers=(30,), name="SDAIA - Archived Regulations", name_ar="سدايا - الأنظمة المؤرشفة",
       authority="Saudi Data & AI Authority",
       url="https://sdaia.gov.sa/ar/SDAIA/about/Pages/ArchivesRegulationsAndPolicies.aspx",
       endpoint="https://sdaia.gov.sa/ar/SDAIA/about/Pages/ArchivesRegulationsAndPolicies.aspx", category="D",
       tier="sectoral", document_kind="archive", connector="page_watch", hosts=("sdaia.gov.sa",),
       default_domain="data_protection",
       access_notes="Archived (historical) material; watched for changes, never treated as current law."),
    # --- E. Judicial decisions and principles ---------------------------------------------------
    _s(key="moj_judgments", numbers=(31,), name="Ministry of Justice - Judicial Decisions",
       name_ar="الأحكام القضائية - وزارة العدل", authority="Ministry of Justice",
       url="https://laws.moj.gov.sa/ar/JudicialDecisionsList/0/",
       endpoint="https://laws-gateway.moj.gov.sa/apis/legislations/v1/Judgements/judgements-list", category="E",
       tier="judicial", document_kind="judicial", connector="moj_judgments_api",
       hosts=("laws.moj.gov.sa", "laws-gateway.moj.gov.sa"), default_domain="judicial", max_items=20,
       access_notes="The portal's public JSON API (judgements-list, get-details) returns ~54,000 anonymised "
                    "judgments with court, city and Hijri/Gregorian dates. Stored as judicial material only."),
    _s(key="bog_judicial", numbers=(32,), name="Board of Grievances - Judicial Materials",
       name_ar="ديوان المظالم - المواد القضائية", authority="Board of Grievances", url="https://www.bog.gov.sa/",
       endpoint="https://www.bog.gov.sa/Pages/default.aspx", category="E", tier="judicial", document_kind="judicial",
       connector="none", hosts=("www.bog.gov.sa",), default_domain="administrative", consolidated_into="bog",
       access_notes="Same endpoint as entry 5; its judicial collections are read by the Board of Grievances connector."),
    _s(key="gstc", numbers=(33,), name="General Secretariat of Zakat, Tax and Customs Committees",
       name_ar="الأمانة العامة للجان الزكوية والضريبية والجمركية", authority="General Secretariat of Zakat, Tax and Customs Committees",
       url="https://gstc.gov.sa/", endpoint="https://gstc.gov.sa/ar/Pages/default.aspx", category="E", tier="judicial",
       document_kind="judicial", connector="page_watch", hosts=("gstc.gov.sa",), default_domain="tax_customs",
       access_notes="Decision search is an interactive service (gstc-fe.zatca.gov.sa); the public page is watched only."),
    _s(key="sjp", numbers=(34,), name="Saudi Scientific Judicial Portal", name_ar="البوابة القضائية العلمية",
       authority="Ministry of Justice", url="https://sjp.moj.gov.sa/", endpoint="https://laws.moj.gov.sa/",
       category="E", tier="judicial", document_kind="judicial", connector="none",
       hosts=("sjp.moj.gov.sa", "laws.moj.gov.sa"), default_domain="judicial", consolidated_into="moj_judgments",
       access_notes="sjp.moj.gov.sa redirects to laws.moj.gov.sa; its judicial content is the Ministry of Justice "
                    "judgments collection integrated through entry 31."),
    # --- F. Supporting sources for legal updates ------------------------------------------------
    _s(key="spa", numbers=(35,), name="Saudi Press Agency", name_ar="وكالة الأنباء السعودية",
       authority="Saudi Press Agency", url="https://www.spa.gov.sa/", endpoint="https://www.spa.gov.sa/", category="F",
       tier="supporting", document_kind="news", connector="page_watch", hosts=("www.spa.gov.sa",),
       access_notes="News: used only to notice possible legal changes. Never legal evidence."),
    _s(key="uqn_council", numbers=(36,), name="Umm Al-Qura - Council of Ministers Decisions",
       name_ar="أم القرى - قرارات مجلس الوزراء", authority="Umm Al-Qura (Official Gazette)",
       url="https://www.uqn.gov.sa/decisions/council-of-ministers-decisions",
       endpoint="https://www.uqn.gov.sa/decisions/council-of-ministers-decisions", category="F", tier="primary",
       document_kind="gazette", connector="html_listing", hosts=("www.uqn.gov.sa", "uqn.gov.sa"),
       link_patterns=(r"^https://www\.uqn\.gov\.sa/decisions-and-regulations/\d+$",), max_items=12,
       access_notes="Server-rendered list of decision pages; the same pages appear in the RSS feed (deduplicated)."),
    _s(key="my_gov_agencies", numbers=(37,), name="Saudi Government Agencies Directory", name_ar="دليل الجهات الحكومية",
       authority="Digital Government Authority (my.gov.sa)", url="https://my.gov.sa/saml.authn/agencies",
       endpoint="https://my.gov.sa/ar/agencies", category="F", tier="supporting", document_kind="directory",
       connector="page_watch", hosts=("my.gov.sa",),
       access_notes="Redirects to /ar/agencies; answered HTTP 403 to automated clients. Reported as blocked."),
    _s(key="my_gov_rules", numbers=(38,), name="Saudi Laws and Regulations Directory", name_ar="دليل الأنظمة واللوائح",
       authority="Digital Government Authority (my.gov.sa)", url="https://my.gov.sa/saml.authn/rules",
       endpoint="https://my.gov.sa/ar/rules", category="F", tier="supporting", document_kind="directory",
       connector="page_watch", hosts=("my.gov.sa",),
       access_notes="Redirects to /ar/rules; answered HTTP 403 to automated clients. Reported as blocked."),
    _s(key="ncc", numbers=(39,), name="National Competitiveness Center", name_ar="المركز الوطني للتنافسية",
       authority="National Competitiveness Center", url="https://ncc.gov.sa/", endpoint="https://scbc.gov.sa/",
       category="F", tier="supporting", document_kind="news", connector="page_watch",
       hosts=("ncc.gov.sa", "scbc.gov.sa"),
       access_notes="ncc.gov.sa redirects to scbc.gov.sa; watched for announcements of regulatory reforms."),
)

BY_KEY = {source.key: source for source in SOURCES}
# Registry names used before the catalog existed; rows created under them are adopted, not duplicated.
LEGACY_NAMES = {
    "Umm Al-Qura RSS - Decisions & Regulations": "uqn_rss",
    "Bureau of Experts - Saudi Laws Portal": "boe",
    "HRSD - Labor Law": "hrsd",
    "Ministry of Commerce - Regulations": "mc",
    "SDAIA - PDPL Knowledge Center": "dgp",
}
CATALOG_HOSTS = frozenset(host for source in SOURCES for host in source.hosts)


def crawl_policy(source: CatalogSource) -> str:
    """Map a connector to the registry's crawl policy (what may be fetched automatically)."""
    if source.connector == "none":
        return "manual"
    if source.connector == "page_watch":
        return "feed_only"  # the page is read for change detection; nothing linked from it is fetched
    return "fetch_allowed"


def integration_status(source: CatalogSource) -> str:
    if source.consolidated_into:
        return "consolidated"
    if source.connector in ("rss", "html_listing", "moj_judgments_api"):
        return "integrated"
    if source.connector == "page_watch":
        return "discovery_only"
    return "not_integrated"


def owner_for_url(url: str) -> CatalogSource | None:
    """The catalog source whose own document pages this URL is (a BOE law page belongs to the BOE)."""
    if re.match(BOE_LAW_PATTERN, url.lower()):
        return BY_KEY["boe"]
    return None
