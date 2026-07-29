# טבלת כיסוי — עורך התגיות של BananaFlow

מיפוי מלא מהקוד הקיים אל העיצוב החדש. **שלב מיפוי בלבד — לא שונה שום קוד.**

מקורות:
- עיצוב: `docs/design/tag-editor/BananaFlow_Tag_Editor_FINAL_COMPLETE.html` (נקרא במלואו, 196 שורות, כולל כל ה־JS והאינטראקציות)
- קוד: `ui/panels/metadata_editor/*` (8,751 שורות), מודלים, בקרים, worker, ליבה

## מקרא עמודות

| # | משמעות |
|---|---|
| 1 | שם הפונקציה / הפעולה |
| 2 | קובץ · מחלקה · סימבול |
| 3 | מיקום בממשק הנוכחי |
| 4 | מיקום בעיצוב החדש |
| 5 | סוג: **ישיר** / **חלון** / **תת־קטגוריה** / **הקשר** / **מצב** |
| 6 | קיים באב־טיפוס? ✅ כן · ⚠️ חלקית · ❌ לא |
| 7 | מה נדרש לשילוב ללא שינוי משמעות |

---

## א. סרגל כלים עליון

| פונקציה | קוד | נוכחי | חדש | סוג | באב־טיפוס | נדרש |
|---|---|---|---|---|---|---|
| ייבוא/ייצוא (IO Hub) | `panel.py:_on_metadata_io` · `_io_btn` | toolbar | תפריט **"עוד" → ייבוא/ייצוא** | חלון | ✅ | להשאיר `_io_btn` כאובייקט (טסטים) גם כשהוא בתפריט |
| שחזור מגיבוי | `panel.py:_on_restore_from_backup` · `_restore_btn` | toolbar | תפריט **"עוד" → שחזור מגיבוי** | חלון | ✅ | — |
| מנהל גיבויים | `panel.py:_on_backup_manager` · `_backup_manager_btn` | toolbar | תפריט **"עוד" → מנהל גיבויים** | חלון | ✅ | — |
| Undo הצעות (Ctrl+Z) | `panel.py:_undo_btn` → `undo_requested` | toolbar | **footer** אייקון | ישיר | ✅ | לשמר `setShortcut("Ctrl+Z")` |
| Redo הצעות (Ctrl+Y) | `panel.py:_redo_btn` → `redo_requested` | toolbar | **footer** אייקון | ישיר | ✅ | לשמר `setShortcut("Ctrl+Y")` |
| סקירת שינויים (Ctrl+Shift+R) | `panel.py:_on_review_changes` · `_review_btn` | toolbar | **footer** "סקירת שינויים" + check→pending | חלון | ✅ | לשמר `Ctrl+Shift+R`; העיצוב לא מציג את הקיצור — להוסיף ל־tooltip |
| ביטול כל השינויים | `panel.py:_on_revert` · `_revert_btn` | toolbar | **footer** "בטל הכל" | ישיר | ✅ | — |
| החלה (Apply) | `panel.py:_on_apply` · `_apply_btn` | toolbar | **footer** primary "החל N שינויים" | חלון | ✅ | חייב להישאר `apply_candidates()` — לא בחירה ולא תצוגה |
| מצב ניטור מערכת קבצים | `panel.py:_monitoring_status` · `on_monitoring_state_changed` | toolbar תווית | toolbar תג **"ניטור פעיל"** | מצב | ✅ | 3 מצבים (פעיל/מושבת/שגיאה) + `diagnostic` ב־tooltip |
| רענון ידני | `panel.py:_manual_refresh_btn` → `manual_refresh_requested` | toolbar | toolbar אייקון refresh | ישיר | ✅ | — |
| סדר אוטומטי | `panel.py:_on_auto_arrange` · `_auto_btn`/`_auto_container` | toolbar (כפתור מפוצל) | **tools → סדר אוטומטי** | ישיר | ✅ | — |
| הגדרות סדר אוטומטי | `panel.py:_on_auto_arrange_settings` · `_auto_cfg_btn` | toolbar (חץ) | **tools → סדר אוטומטי → הגדר** | חלון | ✅ | — |
| בחירת/החלפת תיקייה | `panel.py:_on_browse` · `_browse_btn` | toolbar | toolbar primary "החלף תיקייה" | ישיר | ✅ | הטקסט מתחלף browse/change לפי מצב |
| סריקה מחדש | `panel.py:_on_scan` | (מרומז) | toolbar אייקון refresh | ישיר | ⚠️ | העיצוב ממזג scan+manual refresh — **צריך הכרעה** (ראה קונפליקט C4) |
| מד התקדמות סריקה | `panel.py:_scan_progress` | toolbar | **statecard "סורק…"** + progress | מצב | ✅ | לשמר `_scan_progress` ו־a11y |
| כלול תתי־תיקיות | `panel.py` `scan_requested(Path, bool)` recursive | דיאלוג הבחירה | toolbar **checkbox** | ישיר | ✅ | חדש כ־affordance קבוע — התנהגות זהה |
| נתיב פעיל | `panel.py:_root_folder` | breadcrumbs | toolbar **path-chip (LTR)** | מצב | ✅ | חייב LTR גם בעברית |

## ב. שורת ניווט וטבלה

| פונקציה | קוד | נוכחי | חדש | סוג | באב־טיפוס | נדרש |
|---|---|---|---|---|---|---|
| אחורה / קדימה / למעלה | `_on_navigate_back/_forward/_up` · `TagEditorNavigationState` | navbar | navrow ‹ › ⌃ | ישיר | ✅ | `_refresh_navigation_arrow_direction()` — היפוך ב־RTL |
| Breadcrumbs | `_navigate_breadcrumb` · `_breadcrumbs_layout` | navbar | navrow | ישיר | ✅ | קיצור חכם ברוחב קטן (`.optional`) |
| חיפוש קבצים | `_on_search_text_changed` · `_search_edit` | navbar | **toolbar** searchbox | ישיר | ✅ | סינון תצוגה בלבד — **לא** היקף Apply |
| זום − / ערך / + | `_on_zoom_minus/_plus/_custom` · `_set_zoom` | tbl_head | navrow zoom | ישיר | ✅ | ערך ניתן להקלדה (`_zoom_val_lbl` editable) — לשמר |
| מונה קבצים / מוצגים | `_update_table_info` · `_table_info_lbl` | tbl_head | **tablestatus** תחתון | מצב | ✅ | — |
| תווית היקף Apply | `_apply_scope_lbl` | tbl_head | footer | מצב | ✅ | — |
| החרג / כלול בהחלה | `_toggle_selected_apply_exclusion` · `_exclude_apply_btn` | tbl_head | **תפריט הקשר של שורה** "החרג מהחלה" | הקשר | ✅ | הכפתור מתחלף בין 2 משמעויות — לשמר את שתיהן |
| צ'יפ "הוחרגו (N)" | `_on_excluded_chip_toggled` · `_excluded_chip` | tbl_head | navrow chip | ישיר | ✅ | מסנן תצוגה |
| צ'יפ "חיצוניים (N)" | `_on_stale_chip_toggled` · `_stale_chip` | tbl_head | navrow chip warn | ישיר | ✅ | מסנן תצוגה |
| 15 עמודות | `metadata_table_model.py` `COL_*`, `COLUMN_COUNT=15` | טבלה | 14 נבחרות + gutter | ישיר | ✅ | ראה קונפליקט **C1** |
| בחירת עמודות | `dialogs.py:MoreColumnsDialog` · `_on_more_columns` | header menu → "עוד…" | **חלון בחירת עמודות** (אייקון navrow) | חלון | ✅ | חיפוש עמודה + "תמיד גלוי" ל־filename |
| תפריט הקשר של כותרת | `_on_header_context_menu` | לחיצה ימנית בכותרת | ❌ **חסר** | הקשר | ❌ | **פער G1** — חייב להישאר |
| התאם רוחב לכל העמודות | `_size_all_columns_to_fit` | header menu | ❌ **חסר** | הקשר | ❌ | **פער G1** |
| התאם רוחב לעמודה (dbl-click) | `_size_column_to_fit` · `sectionAutoSizeRequested` | גרירת גבול | לשמר בכותרת | ישיר | ❌ | **פער G1** |
| מיון לפי עמודה | `_on_sort_indicator_changed` | לחיצה בכותרת | לחיצה בכותרת | ישיר | ⚠️ | העיצוב לא מצייר אינדיקטור מיון — להוסיף |
| שינוי רוחב עמודה | `_on_section_resized` + `_fill_leftover_space` | גרירה | גרירה | ישיר | ⚠️ | לשמר עמודת filler ושמירה ל־config |
| גרירת סדר עמודות | `_on_section_moved` · `_save_column_order` | גרירה | גרירה | ישיר | ⚠️ | COL_CHECK ננעל לקצה |
| עריכה בתא | model `setData` (title/artist/album/track/filename/genre/comment new) | dbl-click | לשמר | ישיר | ❌ | **פער G2** — העיצוב מציג טבלה לקריאה בלבד |
| בחירה מרובה / Ctrl / Shift | `ExplorerDetailsView` ExtendedSelection | טבלה | טבלה | ישיר | ✅ | — |
| Rubber-band בחירה | `_select_rows_in_rubber_band` | גרירה בריק | לשמר | ישיר | ❌ | **פער G2** |
| Select-All בכותרת | `MetadataHeaderView.toggled` → `_on_select_all_toggled` | תיבת כותרת | לשמר | ישיר | ❌ | **פער G2** |

### קיצורי מקלדת (טבלה) — `explorer_view.py:keyPressEvent`

| קיצור | פעולה | באב־טיפוס | נדרש |
|---|---|---|---|
| `Ctrl+A` | בחר הכל | ❌ | לשמר |
| `Esc` | נקה בחירה | ⚠️ (רק סגירת חלון) | לשמר את שניהם |
| `F2` | שנה שם (יחיד) | ❌ | לשמר |
| `Enter` | פתח | ❌ | לשמר |
| `Space` | החלף בחירת שורה | ❌ | לשמר |
| `Menu` / `Shift+F10` | תפריט הקשר | ❌ | לשמר |
| `Delete` / `Shift+Delete` | סל מחזור | ❌ | לשמר |
| `Ctrl+Z` / `Ctrl+Y` / `Ctrl+Shift+R` | undo/redo/review | ⚠️ tooltip בלבד | לשמר |

## ג. עץ הקבצים

| פונקציה | קוד | נוכחי | חדש | סוג | באב־טיפוס | נדרש |
|---|---|---|---|---|---|---|
| ניווט לתיקייה | `_on_tree_navigation_item_clicked` | עץ | treepane | ישיר | ✅ | — |
| סימון פריטים | `_on_tree_item_changed` · `_propagate_check_state` | עץ | treepane | ישיר | ⚠️ | העיצוב לא מצייר תיבות סימון — לבדוק |
| גרירה להעברה פיזית | `tree.py:dropEvent` → `_on_tree_item_moved` | עץ | treepane (רמז בעיצוב) | ישיר | ✅ | חייב לעבור דרך `_run_file_operation` |
| קיפול/פתיחת עץ | `_toggle_tree_pane` · `_tree_toggle_btn` | rail | collapsed-tree rail | ישיר | ✅ | — |
| שינוי רוחב | `_body_splitter` + `_save_splitter_sizes` | splitter | splitter | ישיר | ✅ | טווח 160–420 בעיצוב מול `_TREE_OPEN_MIN=210` |
| **תפריט הקשר — עץ** | `_on_tree_context_menu` | | | הקשר | | |
| ↳ פתח | `_file_operations.open_file` | | ✅ | הקשר | ✅ | — |
| ↳ הצג בסייר | `reveal_in_explorer` | | ✅ | הקשר | ✅ | — |
| ↳ העתק נתיב | `_copy_tree_path` | | ✅ | הקשר | ✅ | — |
| ↳ מאפיינים | `_show_path_properties` | | ✅ | הקשר | ✅ | — |
| ↳ הוסף תיקייה | `_on_tree_add_folder` | | ✅ | חלון | ✅ | — |
| ↳ שנה שם | `_on_tree_rename` | | ✅ | חלון | ✅ | — |
| ↳ **העבר אל…** | `_move_tree_path` | | ❌ **חסר** | חלון | ❌ | **פער G3** — הוסר מהתפריט בעיצוב |
| ↳ מחק | `_on_tree_delete` | | ✅ | חלון | ✅ | — |
| קיצור תפריט במקלדת | `tree.py:keyPressEvent` → `keyboardContextMenuRequested` | | לשמר | ישיר | ❌ | **פער G1** |

## ד. פאנל צדדי — מיפוי 8 כלים → 3 מצבים × 15 תת־קטגוריות

| כלי נוכחי (rail) | קוד | תת־קטגוריה חדשה |
|---|---|---|
| `details` (PAGE_TRACKS) | `_build_inspector_tracks` | מתפצל ל־**edit**: fields / artwork / lyrics / gain / props |
| `actions` | `_build_action_engine_page` | **tools → פעולות ותבניות** |
| `from_filename` | `_build_inspector_actions` | **tools → לפי שם הקובץ** |
| `cleanup` | `_build_inspector_actions` (2 מקטעים) | **tools → ניקוי ומחיקה** |
| `files` | `_build_inspector_actions` | **tools → שינוי שמות** |
| `duplicates` | `_build_duplicate_tools_page` | **check → כפילויות** |
| `online` | `_build_online_metadata_page` | **tools → מטא־דאטה מקוון** |
| `problems` | `_build_problems_page` | **check → בעיות** |
| — (חדש) | `_on_auto_arrange` מה־toolbar | **tools → סדר אוטומטי** |
| — (חדש) | `_on_review_changes` + `change_set` | **check → שינויים ממתינים** |
| — (חדש) | `_review_selected_external_conflict` | **check → חיצוניים וחסמים** |
| `PAGE_EMPTY` / `PAGE_FOLDER` | `_build_inspector_empty/_folder` | **statecard** "אין בחירה" | 

### ד1. edit → שדות (21 שדות)

`panel.py:_build_inspector_tracks` `field_specs` — כל 21 קיימים ותואמים 1:1 לעיצוב:

**בסיסיים (10, גלויים):** `title`, `artist`, `album`, `album_artist`, `track_num`, `track_total`, `disc_num`, `disc_total`, `year`, `genre`
**מתקדמים (11, מאחורי "הצג 11 שדות נוספים"):** `comment`, `composer`, `publisher`, `copyright`, `bpm`, `isrc`, `grouping`, `sort_title`, `sort_artist`, `sort_album`, `sort_album_artist`

| פונקציה | קוד | באב־טיפוס | נדרש |
|---|---|---|---|
| ניקוי שדה בודד | `_clear_insp_field` · `_insp_clear_buttons` | ✅ כפתור "נקה" | לשמר a11y per-field |
| סימון dirty | `_mark_insp_field_dirty` · `_insp_field_dirty` | ✅ `.dirty` | — |
| ערכים מעורבים | `meta_mixed_placeholder` | ✅ `.mixed` | — |
| טיוטת עריכה | `_commit_inspector_draft` / `_discard_inspector_draft` · `_insp_draft_item_ids` | ❌ | **פער G4** — קריטי: הטיוטה נצמדת ל־IDs שנבחרו |
| הוסף לשינויים ממתינים | `_on_insp_apply_fields` | ✅ | — |
| קריאה בלבד | `_insp_capability` + `metadata_editable` | ✅ note.warn | — |

### ד2. edit → עטיפה

| פונקציה | קוד | באב־טיפוס | נדרש |
|---|---|---|---|
| תצוגה נוכחית + מוצעת | `_insp_artwork_preview` / `_insp_artwork_proposed_preview` | ✅ | thumbnails דרך `ArtworkThumbnailWorker` |
| גרירה ושחרור | `ArtworkDropPreview` · `_on_artwork_drop` | ✅ | — |
| הוסף | `_on_artwork_add_choose` | ⚠️ | העיצוב נותן "בחר תמונה" אחד |
| החלף | `_on_artwork_replace_choose` | ⚠️ | **פער G5** — Add ו־Replace הן פעולות שונות |
| הסר | `_on_artwork_remove` | ✅ "הצע הסרה" | — |
| הדבק | `_on_artwork_paste` | ✅ | — |
| ייצא | `_on_artwork_export` | ✅ | — |
| בטל הצעה | `_on_artwork_revert` | ❌ | **פער G5** |

### ד3. edit → מילות שיר

| פונקציה | קוד | באב־טיפוס | נדרש |
|---|---|---|---|
| עורך טקסט | `_insp_lyrics` · `_on_lyrics_text_changed` | ✅ | — |
| שפה | `_insp_lyrics_language` | ⚠️ קריאה בלבד בעיצוב | חייב להישאר ניתן לעריכה |
| תיאור | `_insp_lyrics_description` | ⚠️ | חייב להישאר ניתן לעריכה |
| הצע החלפה | `_on_lyrics_propose_set` | ✅ | — |
| הצע ניקוי | `_on_lyrics_propose_clear` | ✅ | — |
| בטל הצעה | `_on_lyrics_revert` | ✅ | — |
| מילים מסונכרנות = קריאה בלבד | `_insp_lyrics_state` | ✅ הערה | — |

### ד4. edit → ReplayGain

| פונקציה | קוד | באב־טיפוס | נדרש |
|---|---|---|---|
| 5 ערכים | `_insp_replay_values` (track gain/peak, album gain/peak, reference) | ✅ | — |
| נתח רצועות | `_on_replaygain_track` → `replaygain_track_requested` | ✅ | — |
| נתח כאלבום | `_on_replaygain_album` → `replaygain_album_requested` | ✅ | — |
| ביטול ניתוח | `_insp_rg_cancel_btn` → `replaygain_cancel_requested` | ✅ | הכפתור מוצג רק בזמן ריצה |
| נקה רצועה | `_on_replaygain_clear_track` | ⚠️ | **פער G6** — העיצוב ממזג ל"הצע ניקוי" אחד |
| נקה אלבום | `_on_replaygain_clear_album` | ⚠️ | **פער G6** |
| בטל הצעה | `_on_replaygain_revert` | ❌ | **פער G6** |
| התקדמות | `_insp_rg_progress` · `on_replaygain_analysis_*` | ✅ | — |

### ד5. edit → מאפייני קובץ

| פונקציה | קוד | באב־טיפוס | נדרש |
|---|---|---|---|
| טבלת מאפיינים | `_insp_properties` · `_format_property_value` | ✅ prop-table | ערכים LTR |
| מצב חיצוני | `_insp_external_status` | ⚠️ | — |
| סקור התנגשות חיצונית | `_insp_external_review_btn` · `_review_selected_external_conflict` | ❌ בטאב זה | **פער G7** — קיים ב־check→externalCheck; לשמר גם כאן או לתעד |
| פתח / הצג בסייר / העתק נתיב | `_open_tracks` / `_reveal_tracks` / `_copy_paths` | ✅ | חדש כ־affordance; הפונקציות קיימות |

### ד6. tools → 17 פעולות (`shared.py:MAGIC_OP_DEFS`)

כל 17 מופו. **כולן פועלות על `workspace.edit_scope()` = שורות נבחרות.**

| קטגוריה בעיצוב | ops |
|---|---|
| לפי שם הקובץ | `title_strip`, `title_full`, `track_num`, `split_at` |
| ניקוי ומחיקה → ניקוי טקסט | `normalize_spaces`, `strip_junk`, `album_artist` |
| ניקוי ומחיקה → ניקוי שדות | `clear_title`, `clear_artist`, `clear_album`, `clear_album_artist`, `clear_track_num`, `clear_year`, `clear_genre`, `clear_comments` |
| שינוי שמות | `clean_filename`, `strip_filename_numbering` |

| נלווה | קוד | באב־טיפוס | נדרש |
|---|---|---|---|
| כפתור מידע לכל פעולה | `_show_info` + `a11y` | ❌ | **פער G8** — 17 כפתורי ⓘ |
| הגדרות ניקוי | `_on_clean_settings` · `CleanSettingsDialog` | ✅ | 8 מתגים |
| שינוי שם לפי כותרת | `_on_insp_rename_from_title` → `rename_from_title` | ✅ | — |
| אמן לתיקייה / אלבום לתיקייה | `_on_insp_folder_artist` / `_on_insp_folder_album` | ❌ | **פער G9** — לא נמצא יעד בעיצוב |

### ד7. tools → מנוע פעולות (`action_dialog.py:TagActionDialog`)

| פונקציה | קוד | באב־טיפוס | נדרש |
|---|---|---|---|
| טאב פעולות / תבניות | `_make_definition_tab` | ✅ | — |
| טאב תהליכים שמורים | `_make_preset_tab` | ✅ | — |
| שמור בשם | `_save_as_preset` | ✅ | — |
| עדכן | `_update_preset` | ✅ | — |
| שנה שם | `_rename_preset` | ✅ | — |
| שכפל | `_duplicate_preset` | ✅ | — |
| מחק | `_delete_preset` | ✅ | — |
| אפס מובנים | `_preset_reset_btn` | ✅ | — |
| ייבוא/ייצוא תהליכים | `_preset_transfer_btn` → IO Hub | ✅ `openIOPresets` | — |
| טווח (scope) | `_scope_arguments` | ✅ 4 ערכים | selection / current / visible / folder |
| תצוגה מקדימה | `refresh_preview` · `_populate_preview_table` | ✅ | — |
| מוני יעדים/נתמכים/שינויים/דולגו/חסמים | `_update_counts` | ✅ 5 תגים | — |
| "שורות שהשתנו בלבד" | filter | ✅ | — |
| חזור לפרמטרים | `_focus_parameters` | ✅ | — |
| הוסף לשינויים ממתינים | `accept_preview` → `_accept_tag_action_preview` | ✅ | חייב לעבור דרך ה־acceptor של ה־Controller |
| אבחון שגיאות | `action_diagnostics.format_action_diagnostic` | ✅ עמודת "פרטים" | — |

### ד8. tools → מטא־דאטה מקוון (`online_metadata_dialog.py`)

| פונקציה | קוד | באב־טיפוס | נדרש |
|---|---|---|---|
| חיפוש MusicBrainz | `_start_search` → `online_search_requested` | ✅ | לא אוטומטי — רק בלחיצה |
| ביטול חיפוש | `_cancel_lookup` → `online_cancel_requested` | ✅ | — |
| מועמדים | `on_lookup_result` · `_candidate_changed` | ✅ | — |
| טבלת השוואה | `on_match_preview` | ✅ | — |
| בחר מומלצים | `_select_recommended` | ✅ | — |
| נקה בחירה | `_clear_fields` | ✅ | — |
| תצוגת עטיפה | `_request_artwork` · `on_artwork_ready` | ✅ | — |
| השתמש בעטיפה | `artwork_use` | ✅ | — |
| הוסף לשינויים | `_add_pending` → `online_accept_requested` | ✅ | — |
| אין תוצאות / רשת / הגבלת קצב / חלקי | `on_artwork_error`, `on_acceptance_error`, `on_online_*` | ✅ 6 מצבים | כל מצב דרך i18n |

### ד9. check → שינויים ממתינים / סקירה

| פונקציה | קוד | באב־טיפוס | נדרש |
|---|---|---|---|
| רשימת ממתינים | `change_sets.py` · `workspace.change_set.records()` | ✅ | — |
| חלון סקירה מלא | `_on_review_changes` | ✅ | — |
| מסננים (הכול/נכללים/הוחרגו/חסומים/ידניים) | `_review_category` | ✅ 5 צ'יפים | — |
| החרג / החזר נבחרים | `review_include_requested` | ✅ | — |
| בטל שדות נבחרים | `review_revert_records_requested` | ✅ | — |
| בטל את כל שינויי הקובץ | `review_revert_files_requested` | ✅ | — |
| ניווט לרשומה | `_navigate_review_record` | ❌ | **פער G10** |
| חסמי Apply | `_review_blocked_records` · `workspace.apply_blockers()` | ✅ | חישוב יחיד קנוני |
| מקור השינוי | `ChangeOrigin` | ✅ עמודת "מקור" | — |

### ד10. check → בעיות

| פונקציה | קוד | באב־טיפוס | נדרש |
|---|---|---|---|
| רשימת בעיות | `_render_problems` · `on_validation_updated` | ✅ | — |
| מסנן חומרה | `_problems_severity` | ✅ | — |
| אמת מחדש | `_begin_revalidate_problems` → `revalidate_problems_requested` | ✅ | — |
| בחר את כל המסוננים | `_filtered_problem_issue_ids` | ✅ | — |
| תקן נבחרים | `_on_fix_selected_problems` | ✅ | — |
| תצוגה מקדימה לתיקון | `_request_problem_fix` · `on_problem_fix_preview` | ✅ חלון | — |
| כשל תצוגה מקדימה | `on_problem_fix_preview_failed` | ❌ | **פער G11** |
| שמירת בחירה | `_remember_problem_selection` | ❌ | **פער G11** |

### ד11. check → כפילויות

| פונקציה | קוד | באב־טיפוס | נדרש |
|---|---|---|---|
| סרוק כפילויות | `_on_find_duplicates` → `find_duplicates_requested` | ✅ | — |
| התקדמות + ETA | `on_duplicate_scan_progress` | ⚠️ | **פער G12** |
| תוצאות + אסטרטגיה | `on_duplicate_scan_complete(groups, elapsed, strategy)` | ⚠️ | — |
| שגיאה | `on_duplicate_scan_error` | ❌ | **פער G12** |
| מחיקה לסל מחזור | `delete_duplicates_requested` · `on_duplicate_delete_complete` | ✅ | — |

### ד12. check → חיצוניים וחסמים

| פונקציה | קוד | באב־טיפוס | נדרש |
|---|---|---|---|
| מונה שינויים חיצוניים | `on_external_changes_updated` · `_set_stale_chip_count` | ✅ | — |
| סקירת התנגשות | `_review_selected_external_conflict` | ✅ חלון | — |
| פתרון התנגשות | `conflict_resolution_requested` · `on_conflict_resolution_finished` | ✅ 3 אפשרויות | — |
| רענון תצוגה | `on_workspace_refresh_applied` | ✅ | — |

## ה. חלונות (25 באב־טיפוס)

| חלון בעיצוב | קוד קיים | סוג | קיים |
|---|---|---|---|
| `columns` | `MoreColumnsDialog` | חלון | ✅ |
| `autoSettings` | `AutoArrangeSettingsDialog` | חלון | ✅ |
| `cleanSettings` | `CleanSettingsDialog` | חלון | ✅ |
| `actionEngine` | `TagActionDialog` | חלון | ✅ |
| `onlineMetadata` | `OnlineMetadataDialog` | חלון | ✅ |
| `ioHub` (7 עמודים) | `MetadataIODialog` — `metadata_export`, `metadata_import`, `change_report`, `problems_report`, `playlist`, `preset_export`, `preset_import` | חלון | ✅ התאמה מדויקת |
| `review` | `_on_review_changes` | חלון | ✅ |
| `backups` | `_on_backup_manager` | חלון | ✅ |
| `restore` | `_on_restore_from_backup` · `on_restore_complete/started/progress` | חלון | ✅ |
| `apply` | `_on_apply` | חלון | ✅ |
| `duplicates` | `_on_find_duplicates` | חלון | ✅ |
| `problemFix` | `on_problem_fix_preview` | חלון | ✅ |
| `externalConflict` | `_review_selected_external_conflict` | חלון | ✅ |
| `recovery` | `on_recovery_available` → `recover_/keep_/forget_recovery_requested` | חלון | ✅ 4 פעולות |
| `draft` | `on_draft_available` → `draft_restore_/discard_requested` | חלון | ✅ |
| `unsaved` | `on_unsaved_changes_action_required` → `unsaved_choice_requested` | חלון | ✅ 3 אפשרויות |
| `undoApplied` | `undo_applied_requested(manifest, explicit_physical)` | חלון | ✅ אישור פיזי מפורש |
| `move` | `_move_tracks` / `_move_tree_path` | חלון | ✅ |
| `rename` | `_rename_tracks` / `_on_tree_rename` | חלון | ✅ |
| `newFolder` | `_on_tree_add_folder` | חלון | ✅ |
| `delete` | `_request_delete_files` / `_on_tree_delete` | חלון | ✅ |
| `properties` | `_show_properties` / `_show_path_properties` | חלון | ✅ |
| `applySuccess` / `applyPartial` / `applyFailure` | `on_apply_batch_complete` · `on_apply_file_outcome` · `on_apply_error` | חלון | ✅ |

## ו. מצבי מערכת (13)

| מצב בעיצוב | קוד | קיים |
|---|---|---|
| `empty` | `_build_table_empty_page` · `_show_table_empty` | ✅ |
| `scanning` | `_set_scan_loading` · `on_scan_progress` | ✅ |
| `emptyFolder` | `on_scan_complete` (0 תוצאות) | ✅ |
| `noSelection` | `PAGE_FOLDER` | ✅ |
| `single` / `mixed` | `_populate_track_inspector` | ✅ |
| `readonly` | `metadata_editable=False` · `TrackStatus.UNSUPPORTED` | ✅ |
| `external` | `is_external_change` · `external_state_blocks_apply` | ✅ |
| `applying` | `_set_apply_loading` · `on_apply_started/progress` | ✅ |
| `success` / `partial` / `failure` | `on_apply_batch_complete` | ✅ |
| `loaded` | `_show_table_content` | ✅ |
| שגיאת סריקה | `on_scan_error` | ⚠️ **פער G13** |
| שחזור בתהליך | `_set_restore_loading` · `on_restore_progress` | ⚠️ **פער G13** |

---

## ז. קונפליקטים הדורשים הכרעה שלך

**C1 — עמודת ה־gutter מול עמודת "מצב".**
בקוד `COLUMN_COUNT = 15`: `COL_CHECK` (0) + 14 עמודות נתונים. `COL_CHECK` היום היא **gutter ויזואלי בלבד** ברוחב 28px — `data()` מחזירה תמיד `Qt.Unchecked`, `setData()` מחזירה `False`, והציור מדלג עליה. היא לא מופיעה בבחירת העמודות ולא בתפריט הכותרת. העיצוב מציג 14 עמודות נבחרות + עמודת **"מצב"** קבועה שאינה קיימת בקוד כעמודה (המידע מוצג היום כגוון שורה + tooltip + צ'יפים).
**שאלה:** להוסיף עמודת "מצב" כעמודה 16 אמיתית, או לצייר אותה כתג בתוך עמודת שם הקובץ?

**C2 — "כלול תתי־תיקיות" כמתג קבוע.**
היום ה־recursive נקבע פעם אחת בבחירת התיקייה (`scan_requested(Path, bool)`). בעיצוב זהו checkbox קבוע בסרגל.
**שאלה:** שינוי המתג יריץ סריקה מחדש מיידית? זה ישנה משמעות (סריקה מחדש = איבוד מצב תצוגה, ואולי טריגר ל"שינויים לא שמורים").

**C3 — חיפוש עבר מ־navrow ל־toolbar.**
טכנית פשוט, אבל `_search_edit` הוא מסנן תצוגה בלבד. בעיצוב מיקומו ליד "החלף תיקייה" עלול לרמז שהוא מסנן סריקה.
**המלצה:** להשאיר במיקום שבעיצוב + להוסיף placeholder/תיאור שמבהיר "סינון התצוגה בלבד".

**C4 — מיזוג "סריקה מחדש" ו"רענון ידני".**
בקוד אלו שתי פעולות שונות: `_on_scan` (סריקה מלאה מחדש) ו־`manual_refresh_requested` (רענון אינקרמנטלי של ניטור מערכת הקבצים). בעיצוב יש אייקון refresh אחד.
**שאלה:** לפצל לשני אייקונים, או אחד עם תפריט?

**C5 — נתיב קובץ העיצוב. פתור.**
הועבר ל־`docs/design/tag-editor/` כפי שביקשת במקור; יתווסף ל־git במסגרת ה־PR.

**C6 — צילומי הייחוס חסרים.**
בתיקייה יש רק את קובץ ה־HTML. אין צילומי מסך. עובד לפי ה־HTML כמקור אמת; אם משהו בעיצוב לא ברור אבקש הבהרה או צילום נקודתי.

---

## ח. פערים באב־טיפוס (פונקציות קיימות שאין להן יעד) — חובה לשלב

| # | פער | יעד מוצע |
|---|---|---|
| G1 | תפריט הקשר של כותרת הטבלה + "התאם רוחב לכולן" + auto-size בלחיצה כפולה + תפריט הקשר במקלדת | לשמר לחיצה ימנית על הכותרת כפי שהיא; הוסף "התאם רוחב" גם לחלון בחירת העמודות |
| G2 | עריכה בתא, rubber-band, Select-All בכותרת | לשמר ב־`ExplorerDetailsView` ללא שינוי |
| G3 | "העבר אל…" בתפריט העץ | להחזיר לתפריט ההקשר של העץ |
| G4 | מנגנון טיוטת העריכה (`_commit/_discard_inspector_draft`) | לשמר; הטיוטה נצמדת ל־item IDs — קריטי לבטיחות |
| G5 | עטיפה: הפרדת Add/Replace + "בטל הצעה" | 4 כפתורים → 6 |
| G6 | ReplayGain: נקה רצועה / נקה אלבום בנפרד + "בטל הצעה" | 4 כפתורים → 6 |
| G7 | כפתור "סקור התנגשות חיצונית" בתוך מאפייני קובץ | לשמר גם ב־edit→props |
| G8 | 17 כפתורי ⓘ להסבר כל פעולה | להוסיף ל־toolcard |
| G9 | "אמן לתיקייה" / "אלבום לתיקייה" (`_on_insp_folder_artist/_album`) | ללא יעד בעיצוב — **צריך החלטה** |
| G10 | ניווט לרשומה מתוך חלון הסקירה | להוסיף לחיצה כפולה בשורה |
| G11 | כשל תצוגה מקדימה לתיקון + שמירת בחירת בעיות | להוסיף למצבי check→בעיות |
| G12 | ETA לסריקת כפילויות + מצב שגיאה | להוסיף למצבי check→כפילויות |
| G13 | מצב שגיאת סריקה + מצב שחזור בתהליך | להוסיף כ־statecards |

---

## ט. אילוץ טסטים

20 קובצי טסט נצמדים ישירות לפנימיות ה־panel. שמות שחייבים לשרוד את העיצוב מחדש (או להתעדכן ביודעין):

`_apply_btn` · `_revert_btn` · `_browse_btn` · `_auto_btn` · `_auto_container` · `_action_engine_btn` · `_manual_refresh_btn` · `_monitoring_status` · `_scan_progress` · `_summary_lbl` · `_apply_scope_lbl` · `_excluded_chip` · `_stale_chip` · `_table` · `_tree` · `_tree_toggle_btn` · `_breadcrumbs_layout` · `_model` · `_proxy` · `_workspace` · `_navigation` · `_root_folder` · `_insp_fields` · `_insp_field_dirty` · `_insp_draft_item_ids` · `_insp_external_status` · `_insp_external_review_btn` · `_inspector_tool_buttons` · `_select_inspector_tool` · `_populate_track_inspector` · `_refresh_checked_scope_state` · `_review_blocked_records` · `_restore_body_sizes` · `_on_apply` · `_on_revert` · `_on_insp_apply_fields` · `_on_online_metadata` · `_on_table_context_menu` · `_on_tree_context_menu` · `_on_tree_rename` · `_on_tree_delete` · `_on_tree_item_moved` · `_rebuild_tree_from_loaded_tracks` · `_apply_display_filter` · `_apply_navigation_filter` · `_create_action_engine_dialog` · `_file_operations`

**המלצה:** לשמר את כל השמות כ־aliases גם כשהווידג'ט עובר מקום, ולעדכן טסט רק כשההתנהגות עצמה משתנה.
