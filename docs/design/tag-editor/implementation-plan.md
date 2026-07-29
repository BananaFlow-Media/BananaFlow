# תוכנית מימוש — עיצוב מחדש של עורך התגיות

ענף: `feat/tag-editor-redesign` · בסיס: `main` @ `e893f60`

## הכרעות שהתקבלו

| # | נושא | הכרעה |
|---|---|---|
| C1 | עמודת "מצב" | עמודה אמיתית חדשה, **ניתנת להסתרה** דרך בחירת העמודות; מיגרציית config מפורטת למטה |
| C2 | כלול תתי־תיקיות | **המתג מוסר לגמרי**; הקוד תמיד סורק רקורסיבית (`recursive=True` קבוע) |
| C4 | רענון | **אייקון אחד + תפריט**: לחיצה = רענון אינקרמנטלי, חץ/תפריט = סריקה מלאה מחדש |
| G9 | אמן/אלבום להיקף | **tools → לפי שם הקובץ** |
| — | סדר אוטומטי | מיושם כלשונו בעיצוב: כפתור ראשי "סדר אוטומטי" **+** כפתור "הגדר אילו פעולות ירוצו" (פותח `AutoArrangeSettingsDialog`) מתחתיו, ואז רשימת "פעולות פעילות". שני הכפתורים הקיימים בקוד (`_auto_btn`, `_auto_cfg_btn`) נשמרים ללא שינוי משמעות. |

### C2 — תיקון עובדתי: לא נדרש שינוי התנהגות
בתוכנית המקורית כתבתי שה־recursive הוא בחירת משתמש ב־`_on_browse` ושהכרעתך מסירה אפשרות קיימת. **זה היה שגוי.**
בפועל `_on_browse` ו־`_on_scan` מעבירים `True` קשיח מאז ומעולם, ואין ולא היה מתג בממשק. כלומר ההתנהגות שביקשת — תמיד לכלול תתי־תיקיות — כבר מתקיימת, ולא נלקחה מהמשתמש שום אפשרות.
מה שנעשה בפועל: לא נוסף ה־checkbox שמופיע באב־הטיפוס, והתנהגות הסריקה נותרה כשהייתה. נוסף טסט (`test_scanning_is_always_recursive`) שנועל את זה כדי שלא ייפרץ בטעות בהמשך.

### מיגרציית config לעמודת המצב (C1) — הבהרה
כשמשתמש קיים כבר שינה סדר/רוחב/הסתרה של עמודות, ה־config שומר זאת כרשימה באורך 15 (`COLUMN_COUNT` הנוכחי). הקוד ב־[panel.py:1313](../../../ui/panels/metadata_editor/panel.py#L1313) בודק `len(saved_order) == COLUMN_COUNT` — ברגע שנוסיף עמודה 16-ית, הבדיקה הזו תיכשל בשקט והמשתמש יאבד את סידור העמודות האישי שלו בפעם הראשונה שהוא פותח את הגרסה החדשה (לא קריסה — רק איפוס לברירת מחדל).
**הפתרון:** מיגרציה מפורשת בטעינת ה־config — אם `len(saved_order) == 15`, מוסיפים את `COL_STATUS` בסוף הרשימה (גלוי כברירת מחדל) במקום לפסול את כל הסדר השמור. אותו טיפול לרשימת ה־visibility. טסט ייעודי מוודא ששדרוג מ־15 ל־16 עמודות לא מאפס הגדרות קיימות.

---

## ארכיטקטורת יעד — פיצול `panel.py`

`panel.py` (5,225 שורות) נשאר **החוזה הציבורי** מול הבקר והטסטים: signals, סלוטים (`on_*`), ושמות הווידג'טים שהטסטים נצמדים אליהם — כ־properties/aliases. הבנייה עוברת למודולים.

```
ui/panels/metadata_editor/
  panel.py                 orchestration + signals + on_* slots + aliases  (יעד ≈900 שורות)
  toolbar.py               TagEditorToolbar — תיקייה, path chip, refresh split, חיפוש, "עוד"
  navigation_bar.py        back/fwd/up, breadcrumbs, צ'יפים, זום, כפתור עמודות
  table_host.py            QStackedWidget: טבלה + state cards + tablestatus
  footer_bar.py            מונה ממתינים, undo/redo, סקירה, בטל הכל, החלה
  state_cards.py           13 המצבים
  workspace_shell.py       3 חלוניות, קיפול, snap, cascade resize  (מ-panel.py)
  inspector/
    shell.py               מצבים (edit/tools/check) + תת־קטגוריות + stack
    edit_fields.py         21 שדות, ניקוי, טיוטה
    edit_artwork.py        6 כפתורים + drop + thumbnails
    edit_lyrics.py         טקסט, שפה, תיאור, 3 כפתורים
    edit_replaygain.py     5 ערכים, 6 כפתורים, התקדמות
    edit_properties.py     מאפיינים + מצב חיצוני + סקירה
    tools_auto.py          כפתור סדר אוטומטי + כפתור הגדרות (AutoArrangeSettingsDialog) + רשימת פעולות פעילות
    tools_actions.py       שער ל-TagActionDialog
    tools_from_filename.py 4 ops + אמן/אלבום להיקף (G9)
    tools_cleanup.py       3 + 8 ops + הגדרות ניקוי
    tools_rename.py        2 ops + שינוי שם לפי כותרת
    tools_online.py        שער ל-OnlineMetadataDialog
    check_pending.py       ממתינים + שער לסקירה
    check_problems.py      מסנן, אמת מחדש, תקן
    check_duplicates.py    סריקה, ETA, שגיאה, מנהל
    check_external.py      חסמים + פתרון התנגשות
```

**ללא שינוי:** `core/*`, `ui/controllers/*`, `ui/workers/*`, `explorer_view.py`, `tree.py`, `action_dialog.py`, `io_dialog.py`, `online_metadata_dialog.py`, `dialogs.py`.
**חריג מוצדק יחיד:** `metadata_table_model.py` + `metadata_filter_proxy_model.py` — עמודת המצב (C1).

---

## שלבים

### שלב 0 — חוזה בטיחות (ללא שינוי ויזואלי)
טסט אפיון שמצלם את מלוא ה-affordances הקיימות: 47 שמות פנימיים, 17 ops, 21 שדות, 3 תפריטי הקשר, 8 קיצורי מקלדת, 25 חלונות.
**קבצים:** `tests/test_tag_editor_surface_contract.py` (חדש)
**בדיקה:** הטסט עובר על הקוד הקיים לפני כל שינוי.

### שלב 1 — פיצול טהור
העברת קוד למודולים ללא שינוי התנהגות. `panel.py` מייבא ומרכיב.
**קבצים:** כל המודולים החדשים + `panel.py`
**בדיקה:** שלב 0 + `test_metadata_editor_toolbar_state`, `test_metadata_inspector_phase6`, `test_metadata_accessibility` — ירוקים ללא שינוי בטסטים.

### שלב 2 — מעטפת: סרגל + footer + 3 חלוניות
סרגל לפי העיצוב (תיקייה, path chip LTR, refresh split, חיפוש, "עוד"); footer חדש עם undo/redo/סקירה/בטל/החל; קיפול ושינוי רוחב.
**מימוש C2:** הסרת מתג ה-recursive מדיאלוג הבחירה, `scan_requested(folder, True)` תמיד.
**מימוש C4:** `QToolButton` עם `InstantPopup` — לחיצה = `manual_refresh_requested`, תפריט = `_on_scan`.
**קבצים:** `toolbar.py`, `footer_bar.py`, `workspace_shell.py`, `panel.py`
**בדיקה:** `test_metadata_editor_toolbar_state`, `test_phase4_folder_navigation_operations`

### שלב 3 — ניווט, טבלה, עמודת מצב
navrow לפי העיצוב; `tablestatus` תחתון; **עמודת מצב חדשה** (C1).
`COLUMN_COUNT` 15 → 16 + מיגרציית `tag_editor_column_order/visibility/widths` ב-config (סדר שמור באורך 15 → הרחבה בטוחה, לא איפוס).
**שימור G1/G2:** תפריט הקשר של הכותרת, "התאם רוחב לכולן", auto-size בלחיצה כפולה, עריכה בתא, rubber-band, Select-All.
**קבצים:** `navigation_bar.py`, `table_host.py`, `metadata_table_model.py`, `metadata_filter_proxy_model.py`, `shared.py`, `dialogs.py`
**בדיקה:** `test_metadata_table_model`, `test_explorer_details_view`, `test_metadata_explorer_verification_matrix` + טסט מיגרציה חדש

### שלב 4 — פאנל צדדי: 3 מצבים × 15 תת־קטגוריות
פיצול `_build_inspector_tracks` ל-5 עמודי edit; 6 עמודי tools; 4 עמודי check.
**שימור G4:** מנגנון הטיוטה (`_insp_draft_item_ids`) חוצה החלפת תת־קטגוריה — commit לפני מעבר, בדיוק כמו היום לפני שינוי בחירה.
**קבצים:** `inspector/*` (16 קבצים), `panel.py`
**בדיקה:** `test_metadata_inspector_phase6`, `test_artwork_inspector_phase7`, `test_lyrics_phase6`, `test_tag_action_ui_phase9`

### שלב 5 — סגירת פערים G3, G5–G13
| פער | פעולה |
|---|---|
| G3 | "העבר אל…" חזרה לתפריט העץ |
| G5 | עטיפה: Add ו-Replace נפרדים + "בטל הצעה" (6) |
| G6 | ReplayGain: נקה רצועה / נקה אלבום נפרדים + "בטל הצעה" (6) |
| G7 | "סקור התנגשות חיצונית" גם ב-edit→מאפיינים |
| G8 | 17 כפתורי ⓘ |
| G9 | אמן/אלבום להיקף ב-tools→לפי שם הקובץ — **תיקון באג**, חיווט ראשון של `artist_to_scope`/`album_to_scope` |
| G10 | ניווט לרשומה בלחיצה כפולה בחלון הסקירה |
| G11 | כשל תצוגה מקדימה + שמירת בחירת בעיות |
| G12 | ETA ומצב שגיאה לסריקת כפילויות |
| G13 | state cards לשגיאת סריקה ולשחזור בתהליך |
**בדיקה:** טסט ממוקד לכל פער; `test_tag_restore`, `test_change_review_phase8`, `test_duplicate_confidence_phase10`

### שלב 6 — 13 מצבים
**קבצים:** `state_cards.py`, `table_host.py`
**בדיקה:** טסט מצבים חדש

### שלב 7 — i18n · RTL · a11y · רספונסיביות
כל מחרוזת חדשה דרך `ui/i18n.py` בעברית ובאנגלית. נתיבים ושמות קבצים LTR גם בעברית. AccessibleName, סדר Tab, Focus, ניגודיות. בדיקה ב-980×680 / 1100×760 / 1440×900.
**קבצים:** `ui/i18n.py` + כל המודולים
**בדיקה:** `test_i18n_coverage`, `test_hardcoded_string_audit`, `test_phase14_accessibility_rtl_dpi`, `test_metadata_accessibility`, `test_accessibility_qss`

### שלב 8 — שער מלא
`python scripts/run_isolated_tests.py` · ביקורת diff מלאה · צילומי מסך בשלושת הרזולוציות × עברית/אנגלית

---

## סיכונים

| סיכון | הפחתה |
|---|---|
| 20 קובצי טסט נצמדים ל-47 שמות פנימיים | שלב 0 קובע חוזה; שלב 1 פיצול טהור; שמות נשמרים כ-aliases |
| `COLUMN_COUNT` 15→16 שובר config שמור | מיגרציה מפורשת + טסט ייעודי |
| `panel.py` 5,225 שורות — פיצול מסוכן | שלב 1 נפרד לגמרי, אפס שינוי התנהגות, טסטים ירוקים לפני המשך |
| טיוטת העריכה חוצה עכשיו גם תת־קטגוריות | commit בכל מעבר, לא רק בשינוי בחירה |
| `run_isolated_tests.py` בלבד (pytest רגיל קורס) | שער מלא רק דרך הראנר המבודד |
