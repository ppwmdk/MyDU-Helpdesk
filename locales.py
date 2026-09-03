# locales.py

TEXTS = {
    "ru": {
        "choose_lang": "👋 Сәлеметсіз бе! Здравствуйте! Hello!\n\nТілді таңдаңыз / Выберите язык / Choose language:",
        "lang_saved": "✅ Язык интерфейса успешно установлен: Русский",
        "welcome": (
            "✨ <b>Добро пожаловать в службу поддержки Astana IT University!</b>\n\n"
            "Здесь вы можете изучить инструкции или отправить заявку при возникновении неполадок."
        ),
        "btn_report": "📝 Подать заявку",
        "btn_guides": "📚 Инструкции и памятки",
        "btn_my_reports": "📂 Мои заявки",
        "btn_change_lang": "🌐 Тіл / Язык / Lang",
        "btn_cancel": "❌ Отмена",
        "btn_skip": "Пропустить ➡️",
        
        "enter_fio": "Введите ваши <b>ФИО</b>:",
        "enter_group": "Введите вашу <b>академическую группу</b> (например, IT-2101):",
        "choose_module": "Выберите <b>категорию (модуль)</b> вопроса:",
        "enter_desc": "Опишите вашу проблему <b>подробно</b>:",
        "send_screen": "Прикрепите <b>скриншот ошибки</b> (или нажмите «Пропустить ➡️»):",
        "action_cancelled": "Действие отменено.",
        
        "report_success": (
            "✅ <b>Заявка #{id} успешно зарегистрирована!</b>\n\n"
            "Категория: {module}\n"
            "Мы уведомим вас, как только специалисты ответят на неё."
        ),
        
        "guides_title": "📚 <b>База знаний и инструкции</b>\nВыберите интересующую вас тему:",
        "dorm_guide_btn": "🏢 Общежитие (Пошаговая инструкция)",
        "dorm_caption": "🏢 <b>Инструкция по общежитию (Страницы 1–8)</b>\nИзучите прикреплённые карточки по порядку.",
        "guides_empty": "В данном разделе пока нет инструкций.",
        "back_to_guides": "⬅️ Назад к темам",
        
        "my_reports_empty": "У вас пока нет поданных заявок.",
        "my_reports_header": "📂 <b>Ваши последние заявки:</b>\n\n",
        "ticket_item": "• <b>Тикет #{id}</b> [{status}]\n  Модуль: {module}\n  Дата: {date}\n",
        
        "reply_notification": "✉️ <b>Новый ответ по заявке #{id} от техподдержки:</b>\n\n{text}",
        "status_notification": "🔔 Статус вашей заявки #{id} изменен на: <b>{status}</b>"
    },
    "kz": {
        "choose_lang": "👋 Сәлеметсіз бе! Здравствуйте! Hello!\n\nТілді таңдаңыз / Выберите язык / Choose language:",
        "lang_saved": "✅ Тіл сәтті орнатылды: Қазақша",
        "welcome": (
            "✨ <b>Astana IT University қолдау қызметіне қош келдіңіз!</b>\n\n"
            "Мұнда сіз нұсқаулықтарды қарап, мәселелер бойынша өтініш бере аласыз."
        ),
        "btn_report": "📝 Өтініш беру",
        "btn_guides": "📚 Нұсқаулықтар",
        "btn_my_reports": "📂 Менің өтініштерім",
        "btn_change_lang": "🌐 Тіл / Язык / Lang",
        "btn_cancel": "❌ Бас тарту",
        "btn_skip": "Өткізіп жіберу ➡️",
        
        "enter_fio": "Толық <b>аты-жөніңізді (ФИО)</b> енгізіңіз:",
        "enter_group": "<b>Академиялық тобыңызды</b> енгізіңіз (мысалы, IT-2101):",
        "choose_module": "Сұрақтың <b>санатын (модулін)</b> таңдаңыз:",
        "enter_desc": "Мәселені <b>толық сипаттап</b> жазыңыз:",
        "send_screen": "Қатенің <b>скриншотын</b> жіберіңіз (немесе «Өткізіп жіберу ➡️» батырмасын басыңыз):",
        "action_cancelled": "Әрекет тоқтатылды.",
        
        "report_success": (
            "✅ <b>Өтініш #{id} сәтті тіркелді!</b>\n\n"
            "Санат: {module}\n"
            "Мамандар жауап берген кезде сізге хабарлама жіберіледі."
        ),
        
        "guides_title": "📚 <b>Білім базасы және нұсқаулықтар</b>\nҚажетті тақырыпты таңдаңыз:",
        "dorm_guide_btn": "🏢 Жатақхана (Қадамдық нұсқаулық)",
        "dorm_caption": "🏢 <b>Жатақхана нұсқаулығы (1–8 беттер)</b>\nСуреттерді ретімен қарап шығыңыз.",
        "guides_empty": "Бұл бөлімде әзірге нұсқаулықтар жоқ.",
        "back_to_guides": "⬅️ Тақырыптарға қайту",
        
        "my_reports_empty": "Сізде әзірге белсенді өтініштер жоқ.",
        "my_reports_header": "📂 <b>Сіздің соңғы өтініштеріңіз:</b>\n\n",
        "ticket_item": "• <b>Өтініш #{id}</b> [{status}]\n  Санат: {module}\n  Уақыты: {date}\n",
        
        "reply_notification": "✉️ <b>#{id} өтініші бойынша техникалық қолдаудан жаңа жауап:</b>\n\n{text}",
        "status_notification": "🔔 Сіздің #{id} өтінішіңіздің мәртебесі өзгертілді: <b>{status}</b>"
    },
    "en": {
        "choose_lang": "👋 Сәлеметсіз бе! Здравствуйте! Hello!\n\nТілді таңдаңыз / Выберите язык / Choose language:",
        "lang_saved": "✅ Interface language set to: English",
        "welcome": (
            "✨ <b>Welcome to Astana IT University Support Bot!</b>\n\n"
            "Here you can browse knowledge guides or submit issue tickets."
        ),
        "btn_report": "📝 Submit Ticket",
        "btn_guides": "📚 Guides & FAQs",
        "btn_my_reports": "📂 My Tickets",
        "btn_change_lang": "🌐 Тіл / Язык / Lang",
        "btn_cancel": "❌ Cancel",
        "btn_skip": "Skip ➡️",
        
        "enter_fio": "Enter your <b>Full Name</b>:",
        "enter_group": "Enter your <b>academic group</b> (e.g. IT-2101):",
        "choose_module": "Select the issue <b>category (module)</b>:",
        "enter_desc": "Describe your problem <b>in detail</b>:",
        "send_screen": "Attach a <b>screenshot</b> (or click 'Skip ➡️'):",
        "action_cancelled": "Action cancelled.",
        
        "report_success": (
            "✅ <b>Ticket #{id} successfully registered!</b>\n\n"
            "Category: {module}\n"
            "You will be notified once support staff replies."
        ),
        
        "guides_title": "📚 <b>Knowledge Base & Guides</b>\nSelect a topic:",
        "dorm_guide_btn": "🏢 Dormitory (Step-by-step Guide)",
        "dorm_caption": "🏢 <b>Dormitory Guide (Pages 1–8)</b>\nPlease check attached images in sequential order.",
        "guides_empty": "No guides available in this section yet.",
        "back_to_guides": "⬅️ Back to topics",
        
        "my_reports_empty": "You have no submitted tickets yet.",
        "my_reports_header": "📂 <b>Your recent tickets:</b>\n\n",
        "ticket_item": "• <b>Ticket #{id}</b> [{status}]\n  Module: {module}\n  Date: {date}\n",
        
        "reply_notification": "✉️ <b>New reply regarding ticket #{id}:</b>\n\n{text}",
        "status_notification": "🔔 Status of your ticket #{id} changed to: <b>{status}</b>"
    }
}

def t(lang: str, key: str, **kwargs) -> str:
    lang = lang if lang in TEXTS else "ru"
    tmpl = TEXTS[lang].get(key, TEXTS["ru"].get(key, key))
    if kwargs:
        return tmpl.format(**kwargs)
    return tmpl