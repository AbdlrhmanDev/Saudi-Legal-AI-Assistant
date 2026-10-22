"""Saudi-dialect legal intent understanding.

Every case is a realistic way a person in Saudi Arabia might ask: Najdi, Hijazi, Eastern and Southern
phrasing, spelling mistakes, Arabizi, English, and mixed Arabic-English. Each must be recognised as a
Saudi legal question (in scope) and mapped to one of the listed legal intents within the top two.
The intent decides which formal legal vocabulary is searched; it never supplies a legal rule.
"""

import pytest

from legal_core.intent import INTENT_BY_ID, analyze_query, dialect_normalize, infer_domain

# (question, acceptable intent ids)
CASES = [
    # --- labor: termination ---------------------------------------------------------------
    ("مديري فصلني بدون سبب وش أسوي؟", {"labor.termination"}),
    ("طردوني من الشغل فجأة بدون اي انذار", {"labor.termination"}),
    ("الشركه فنشتني وانا لي ٣ سنين عندهم", {"labor.termination"}),
    ("ابغى اعرف حقوقي اذا الشركة سرحتني", {"labor.termination"}),
    ("انهوا عقدي قبل لا يخلص هل لي تعويض", {"labor.termination"}),
    ("They dismissed me without notice, what are my rights in Saudi?", {"labor.termination"}),
    ("مديري يبي يفصلني عشان طالبت بحقي", {"labor.termination"}),
    ("انطردت من الدوام وما عطوني ورقه", {"labor.termination"}),
    # --- labor: wages -----------------------------------------------------------------------
    ("الشركة ما عطتني راتبي من شهرين", {"labor.unpaid_wages"}),
    ("صار لي ٣ شهور بدون راتب وش الحل", {"labor.unpaid_wages"}),
    ("رواتبنا متأخره والمدير يقول اصبروا", {"labor.unpaid_wages"}),
    ("my salary is delayed for 2 months, what can i do", {"labor.unpaid_wages"}),
    ("ma3tani rateb mn shahrain", {"labor.unpaid_wages"}),
    ("الكفيل ماسك راتبي وما يبي يعطيني", {"labor.unpaid_wages", "labor.transfer_sponsorship"}),
    ("خصمو من راتبي بدون سبب", {"labor.unpaid_wages"}),
    ("الشركة ماتدفع الاوفر تايم", {"labor.working_hours", "labor.unpaid_wages"}),
    ("متى لازم تنزل الرواتب بالحساب؟", {"labor.unpaid_wages"}),
    # --- labor: end of service ------------------------------------------------------------
    ("كم لي مكافأة إذا استقلت؟", {"labor.end_of_service", "labor.resignation"}),
    ("ياخي مديري ما عطاني مستحقاتي بعد ما طلعت من الشغل، وش أسوي؟", {"labor.end_of_service", "labor.unpaid_wages"}),
    ("كيف احسب مكافئة نهاية الخدمه", {"labor.end_of_service"}),
    ("اشتغلت ٧ سنين وابي اعرف كم نهاية الخدمة حقتي", {"labor.end_of_service"}),
    ("end of service calculation if i resign after 4 years", {"labor.end_of_service", "labor.resignation"}),
    ("مستحقاتي النهائيه ما صرفوها", {"labor.end_of_service", "labor.unpaid_wages"}),
    ("mukafaa nihayat al khidma kam", {"labor.end_of_service"}),
    ("تخلصت من الشغل ولا عطوني ولا ريال من حقوقي", {"labor.unpaid_wages", "labor.end_of_service"}),
    # --- labor: resignation, hours, leave, probation ----------------------------------------
    ("ابي استقيل كم لازم اعطيهم مهله", {"labor.resignation"}),
    ("قدمت استقالتي والمدير رافض يقبلها", {"labor.resignation"}),
    ("اذا استقلت وانا تحت التجربة وش يصير", {"labor.resignation", "labor.probation"}),
    ("i want to resign, how long is the notice period?", {"labor.resignation"}),
    ("بترك الشغل الشهر الجاي هل لازم اشعار", {"labor.resignation"}),
    ("كم ساعه الدوام الرسمي باليوم؟", {"labor.working_hours"}),
    ("يشغلونا ١٢ ساعة يومياً هل هذا نظامي؟", {"labor.working_hours"}),
    ("دوامي في رمضان كم ساعة المفروض", {"labor.working_hours"}),
    ("overtime pay in saudi labor law", {"labor.working_hours"}),
    ("ما فيه يوم راحه في الاسبوع، يحق لهم؟", {"labor.working_hours"}),
    ("كم يوم اجازتي السنويه؟", {"labor.leave"}),
    ("تعبت وطلعت مرضيه، يخصمون علي؟", {"labor.leave"}),
    ("زوجتي ولدت كم اجازه الولاده للاب", {"labor.leave"}),
    ("annual leave days for employees in KSA", {"labor.leave"}),
    ("رفضوا يعطوني اجازه وصار لي سنتين ما اخذت", {"labor.leave"}),
    ("كم مدة فترة التجربة؟", {"labor.probation"}),
    ("انا تحت التجربه ويبون يمددونها ٦ شهور", {"labor.probation"}),
    ("probation period max in saudi", {"labor.probation"}),
    # --- labor: injury, transfer, absence, domestic, harassment ---------------------------------
    ("انصبت في الشغل وطاحت علي حديده من يتحمل العلاج", {"labor.work_injury"}),
    ("صار لي حادث عمل في الموقع", {"labor.work_injury"}),
    ("injured at work who pays", {"labor.work_injury"}),
    ("ابي انقل كفالتي بدون موافقة الكفيل", {"labor.transfer_sponsorship"}),
    ("كيف اسوي نقل خدمات وانا عقدي ما خلص", {"labor.transfer_sponsorship"}),
    ("can i change sponsor without approval? kafala", {"labor.transfer_sponsorship"}),
    ("kafeel rafadh ynagilni", {"labor.transfer_sponsorship"}),
    ("الكفيل حطني هروب وانا ما سويت شي", {"labor.absence_report"}),
    ("رفعوا علي بلاغ تغيب عن العمل كيف الغيه", {"labor.absence_report"}),
    ("الشغالة رفضت تشتغل وتبي تسافر", {"labor.domestic_worker"}),
    ("السواق حقنا هرب", {"labor.domestic_worker", "labor.absence_report"}),
    ("زميلي في العمل يتحرش فيني", {"labor.discrimination_harassment"}),
    # --- social insurance ---------------------------------------------------------------------
    ("ليش التأمينات ما سجلتني الشركة فيها", {"social_insurance.gosi"}),
    ("كم معاش التقاعد لو تقاعدت مبكر", {"social_insurance.gosi"}),
    ("طلعت من الشغل هل يحق لي ساند", {"social_insurance.gosi"}),
    ("gosi registration not done by employer", {"social_insurance.gosi"}),
    # --- real estate --------------------------------------------------------------------------
    ("المؤجر يبي يطلعني من الشقة فجأة", {"real_estate.eviction"}),
    ("صاحب العماره يبغا يطلعنا قبل نهاية العقد", {"real_estate.eviction", "real_estate.lease"}),
    ("landlord wants to evict me in 10 days", {"real_estate.eviction"}),
    ("المالك قطع علي الكهربا عشان اطلع", {"real_estate.eviction"}),
    ("e5la2 shaqa bdoon e5bar", {"real_estate.eviction"}),
    ("زادوا علي الإيجار فجأة هل يحق لهم", {"real_estate.lease"}),
    ("المستأجر ما دفع الايجار من ٤ شهور وش اسوي", {"real_estate.lease"}),
    ("عقد الايجار مو موثق في ايجار هل هو نظامي", {"real_estate.lease"}),
    ("صاحب الشقه ما رجع لي التامين", {"real_estate.lease"}),
    ("my landlord refuses to return the deposit", {"real_estate.lease"}),
    ("شريت ارض وطلع الصك فيه مشكلة", {"real_estate.ownership"}),
    ("مكتب العقار ياخذ سعي ٥٪ هل هذا مسموح", {"real_estate.ownership"}),
    ("off-plan apartment delayed by the developer, title deed not issued", {"real_estate.ownership"}),
    # --- consumer -----------------------------------------------------------------------------
    ("المطعم رفض يرجع فلوسي رغم إن الطلب مخالف، وش حقوقي؟", {"consumer.refund"}),
    ("شريت جوال وطلع فيه عيب والمحل رافض يبدله", {"consumer.refund", "consumer.warranty"}),
    ("طلبت من موقع وما وصلني الطلب وما يردون", {"consumer.refund"}),
    ("the store won't refund my money", {"consumer.refund"}),
    ("اشتريت ثلاجة وخربت بعد اسبوع والضمان ما يغطي", {"consumer.warranty", "consumer.refund"}),
    ("اعلان مضلل عن تخفيضات وهمية في محل", {"consumer.refund"}),
    ("المحل ما رضى يرجع فلوسي", {"consumer.refund"}),
    ("مطعم قدم لي أكل فاسد وتسممت، هل أقدر أشتكي؟", {"consumer.refund", "judicial.lawsuit"}),
    ("شركة الطيران ألغت رحلتي ورفضت تعوضني", {"consumer.refund"}),
    # --- commercial ---------------------------------------------------------------------------
    ("فتحت متجر إلكتروني وش الأشياء اللي لازم أسويها؟", {"commercial.ecommerce"}),
    ("ابيع عطور في الانستقرام هل احتاج سجل تجاري", {"commercial.ecommerce", "commercial.company_formation"}),
    ("i opened an online store in saudi what licenses do i need", {"commercial.ecommerce"}),
    ("متجري على سلة لازم اسجل في معروف؟", {"commercial.ecommerce"}),
    ("ابي افتح مؤسسة صغيرة وش المطلوب", {"commercial.company_formation"}),
    ("كيف اطلع سجل تجاري لمحل", {"commercial.company_formation"}),
    ("how to set up an LLC in saudi arabia as a citizen", {"commercial.company_formation"}),
    ("وثيقة العمل الحر تكفي ولا لازم سجل", {"commercial.company_formation"}),
    ("كم الحد الأدنى لرأس مال شركة المساهمة", {"commercial.corporate_governance"}),
    ("شريكي ياخذ الارباح كلها لحاله", {"commercial.partners", "commercial.corporate_governance"}),
    ("واحد اجنبي يشغل المحل باسمي هل هذا تستر", {"commercial.cover_up"}),
    ("عقوبة التستر التجاري كم", {"commercial.cover_up"}),
    ("عطاني شيك وطلع بدون رصيد", {"commercial.cheque_debt"}),
    ("واحد مديون لي ٥٠ الف وما يبي يرجعها", {"commercial.cheque_debt"}),
    ("سلفت صديقي فلوس وانكر", {"commercial.cheque_debt"}),
    ("شركتي متعثره ابي اعرف عن الافلاس", {"commercial.bankruptcy"}),
    # --- criminal / cyber ---------------------------------------------------------------------
    ("واحد نصب علي بحراج وحولت له فلوس", {"criminal.fraud"}),
    ("انضحك علي في متجر وهمي", {"criminal.fraud"}),
    ("someone scammed me on haraj after i transferred money", {"criminal.fraud"}),
    ("جاني رابط وهمي وسحبوا من حسابي", {"criminal.fraud", "finance.banking"}),
    ("nasab 3alai wa7ed fe haraj", {"criminal.fraud"}),
    ("واحد يبتزني بصوري وش اسوي", {"criminal.cyber_extortion"}),
    ("هكروا حسابي في الانستقرام", {"criminal.hacking"}),
    ("واحد سبني في تويتر وابي اشتكي", {"criminal.defamation"}),
    ("someone is blackmailing me with private photos", {"criminal.cyber_extortion"}),
    ("نشروا عني كلام كذب في القروب", {"criminal.defamation"}),
    # --- traffic ------------------------------------------------------------------------------
    ("جتني مخالفة ساهر وانا ما كنت مسرع", {"traffic.violation"}),
    ("كم مخالفة قطع الإشارة؟", {"traffic.violation"}),
    ("صدمني واحد وهرب وش اسوي", {"traffic.accident"}),
    ("سحبوا رخصتي بسبب التفحيط", {"traffic.violation"}),
    ("speeding ticket in riyadh how much", {"traffic.violation"}),
    # --- family -------------------------------------------------------------------------------
    ("زوجي يبي يطلقني وياخذ العيال", {"family.divorce", "family.custody"}),
    ("ابي اخلع زوجي وش الاجراءات", {"family.divorce"}),
    ("طليقي ما يصرف على العيال", {"family.alimony"}),
    ("how is inheritance divided between brothers and sisters in saudi", {"family.inheritance"}),
    ("ابوي توفى وفيه خلاف على الورث", {"family.inheritance"}),
    # --- privacy, tax, IP ---------------------------------------------------------------------
    ("شركة تتصل علي وتعرف بياناتي من وين جابوها", {"privacy.personal_data"}),
    ("الموظف سرب رقمي وصوري", {"privacy.personal_data"}),
    ("data breach notification requirements under pdpl", {"privacy.personal_data"}),
    ("متى لازم اسجل في ضريبة القيمة المضافة؟", {"tax.vat"}),
    ("الجمارك حجزت شحنتي", {"tax.customs"}),
    ("is VAT 15% on all services in saudi", {"tax.vat"}),
    ("واحد سرق تصاميمي ونشرها باسمه", {"ip.copyright"}),
    ("ابي اسجل علامة تجارية لمشروعي", {"ip.trademark_patent"}),
    ("سرقوا كود برنامجي ونشروه باسمهم", {"ip.copyright"}),
    # --- residency, administrative, judicial ----------------------------------------------------
    ("اقامتي خلصت وابي اجددها والكفيل رافض", {"residency.iqama_visa", "labor.transfer_sponsorship"}),
    ("كم رسوم المرافقين؟", {"residency.iqama_visa"}),
    ("final exit visa process for expats", {"residency.iqama_visa"}),
    ("ابي اتظلم من قرار الجهة الحكومية", {"administrative.grievance"}),
    ("قرار إداري ظالم كيف اعترض عليه", {"administrative.grievance"}),
    ("كيف ارفع قضية على واحد في ناجز", {"judicial.lawsuit"}),
    ("كيف اطلب تنفيذ حكم صادر لي", {"judicial.enforcement"}),
    ("اوقفوا خدماتي بسبب دين", {"judicial.enforcement", "commercial.cheque_debt"}),
    ("ابي اسوي وكالة لاخوي يخلص عني", {"judicial.power_of_attorney"}),
    # --- health, municipal, finance, insurance, investment, competition --------------------------
    ("الدكتور غلط في العملية وصار عندي مضاعفات", {"health.malpractice"}),
    ("جاري بنى ملحق وسد علي الشمس", {"municipal.building"}),
    ("البنك خصم من حسابي رسوم ما اعرفها", {"finance.banking"}),
    ("شركة التأمين رفضت تعوضني عن الحادث", {"insurance.claim", "consumer.refund"}),
    ("ابي استثمر في السعودية وانا اجنبي", {"investment.foreign"}),
    ("احتكار في السوق وش الجهة المختصة", {"competition.antitrust"}),
]


def test_the_dataset_is_large_and_covers_many_domains():
    assert len(CASES) >= 100
    domains = {INTENT_BY_ID[next(iter(expected))].domain for _q, expected in CASES}
    assert len(domains) >= 14
    for _question, expected in CASES:
        assert expected <= INTENT_BY_ID.keys()


@pytest.mark.parametrize(("question", "expected"), CASES, ids=[f"case{i:03d}" for i in range(len(CASES))])
def test_colloquial_question_is_in_scope_and_understood(question, expected):
    analysis = analyze_query(question)
    assert analysis.in_scope, (question, analysis.scope_reason, analysis.signals)
    top_two = {match.id for match in analysis.intents[:2]}
    assert expected & top_two, (question, [m.id for m in analysis.intents])
    # Every recognised intent contributes a formal legal search query; the user's text is untouched.
    assert analysis.expansions and analysis.original == question


def test_specification_examples_map_to_the_described_legal_issue():
    termination = analyze_query("مديري فصلني بدون سبب وش أسوي؟")
    assert termination.intents[0].id == "labor.termination" and termination.domain == "labor"
    assert "إنهاء عقد العمل" in termination.expansions[0]
    assert termination.register == "saudi_dialect"

    wages = analyze_query("الشركة ما عطتني راتبي من شهرين")
    assert wages.intents[0].id == "labor.unpaid_wages"
    assert {"amount": "2", "unit": "شهر"} in wages.entities["durations"]

    eviction = analyze_query("المؤجر يبي يطلعني من الشقة فجأة")
    assert eviction.intents[0].id == "real_estate.eviction" and eviction.domain == "real_estate"

    fraud = analyze_query("واحد نصب علي بحراج وحولت له فلوس")
    assert fraud.intents[0].id == "criminal.fraud"

    ecommerce = analyze_query("فتحت متجر إلكتروني وش الأشياء اللي لازم أسويها؟")
    assert ecommerce.intents[0].id == "commercial.ecommerce"

    gratuity = analyze_query("كم لي مكافأة إذا استقلت؟")
    assert gratuity.intents[0].id == "labor.end_of_service"
    # Only facts that change the applicable rule are asked for.
    assert any("مدة الخدمة" in fact for fact in gratuity.facts_to_confirm)

    final = analyze_query("ياخي مديري ما عطاني مستحقاتي بعد ما طلعت من الشغل، وش أسوي؟")
    assert final.in_scope and final.domain == "labor" and final.register == "saudi_dialect"


def test_registers_are_detected():
    assert analyze_query("وش حقوقي اذا فصلوني؟").register == "saudi_dialect"
    assert analyze_query("ما مدة فترة التجربة في نظام العمل؟").register == "msa"
    assert analyze_query("What are my rights if my employer fired me?").register == "english"
    assert analyze_query("my company ما عطتني salary من شهرين").register == "mixed"
    assert analyze_query("mudeer fa9alni wesh asawi").register == "arabizi"


def test_arabizi_and_spelling_variants_normalize_to_searchable_arabic():
    assert "فصلني" in dialect_normalize("mudeer fa9alni")
    assert "مكافاه" in dialect_normalize("مكافئة نهاية الخدمة")
    mixed = analyze_query("my company ما عطتني salary من شهرين")
    assert mixed.in_scope and mixed.intents[0].id == "labor.unpaid_wages"


def test_entities_are_extracted_without_changing_the_question():
    analysis = analyze_query("اشتغلت ٥ سنوات براتب 8000 ريال وعقدي غير محدد المدة، كم مكافأتي حسب المادة 84؟")
    assert {"amount": "5", "unit": "سنوات"} in analysis.entities["durations"]
    assert analysis.entities["amounts_sar"] == ["8000"]
    assert analysis.entities["contract_type"] == "indefinite"
    assert analysis.entities["article_numbers"] == ["84"]


def test_draft_and_historical_requests_are_flagged():
    assert analyze_query("هل فيه مشروع نظام جديد للإيجار مطروح في استطلاع؟").wants_draft
    assert analyze_query("وش كان نظام العمل يقول قبل التعديل عن الاجازات؟").wants_historical
    assert not analyze_query("كم يوم اجازتي السنويه؟").wants_draft


@pytest.mark.parametrize(
    ("title", "domain"),
    [
        ("نظام العمل", "labor"),
        ("اللائحة التنفيذية لنظام العمل", "labor"),
        ("نظام الشركات", "commercial"),
        ("نظام حماية البيانات الشخصية", "data_protection"),
        ("نظام ضريبة القيمة المضافة", "tax_customs"),
        ("نظام التسجيل العيني للعقار", "real_estate"),
        ("نظام التأمينات الاجتماعية", "social_insurance"),
        ("نظام مكافحة الجرائم المعلوماتية", "criminal"),
        ("نظام العلامات التجارية", "intellectual_property"),
        ("نظام المرافعات الشرعية", "judicial"),
        ("نظام المنافسة", "competition"),
        ("قواعد التامين الشامل للمركبات", "insurance"),
    ],
)
def test_document_domains_are_inferred_from_official_titles(title, domain):
    assert infer_domain(title) == domain


def test_unknown_titles_get_no_guessed_domain():
    assert infer_domain("قرار مجلس الوزراء رقم 12") is None
    assert infer_domain(None) is None
