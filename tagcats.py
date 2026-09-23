# -*- coding: utf-8 -*-
"""提示词分类 —— 规则+关键词自动分类（danbooru tag -> 一级/二级）

0923 改版：一级从 22 类砍到 8 类（GPT 架构评审结论），原 22 类降级为二级。
- 用户视角只看到 8 个 Tab：「人物 / 服装 / 动作 / 构图 / 场景 / 道具 / 画面 / 涩涩」
- 每类下面的二级横排是 filter，不是新页面（数据层保留复杂分类，UI 不能）
- 不手工分类：3 万个 tag，人工分类会烂尾。规则覆盖高频 80%+，长尾自然落空
- 天然多归属：一个 tag 命中几条规则就进几个二级（如 hair_between_eyes = 头发+眼睛）
- 正则都作用在小写、下划线形式的英文 tag 上

数据来源词库：amenorira/danbooru-tags-data-zh（MIT，2026-08 数据），路径见 config.json 的 tag_dir
"""

# ── 一级 → 二级 归属表（顺序 = 面板显示顺序）──
PRIMARY = [
    ("人物", ["人数", "身体", "脸部", "头发", "眼睛", "非人"]),
    ("服装", ["上衣", "下装", "细节", "内衣袜", "鞋配饰", "制服"]),
    ("动作", ["动作"]),
    ("构图", ["视角", "尺寸"]),
    ("场景", ["室内", "室外", "背景", "天气"]),
    ("道具", ["道具"]),
    ("画面", ["光线", "画风"]),
    ("涩涩", ["涩涩"]),
]

# ── (二级名, 正则) —— 一个 tag 可命中多条 ──
RULES = [
    ("人数", r"^(1girl|1boy|2girls|2boys|3girls|multiple_girls|multiple_boys|solo|duo|group|couple|"
             r"male_focus|female_focus|everyone|no_humans|crowd|hetero|yuri|yaoi)$"),

    ("尺寸", r"(^|_)(full_body|upper_body|lower_body|cowboy_shot|portrait|close-?up|"
             r"head_out_of_frame|feet_out_of_frame|shot)$"),

    ("视角", r"(^|_)(from_above|from_below|from_behind|from_side|from_below|from_outside|pov|"
             r"looking_at_another|looking_to_the_side|"
             r"dutch_angle|wide_shot|foreshortening|perspective|looking_at_viewer|"
             r"looking_back|looking_down|looking_up|looking_away|eye_contact|"
             r"depth_of_field|blurry_background|bokeh|motion_blur)(_|$)"),

    ("头发", r"(^|_)(hair|hairstyle|bangs|braid|ponytail|twintails|twin_braids|ahoge|hime_cut|"
             r"bob_cut|curly_hair|wavy_hair|long_hair|short_hair|medium_hair|messy_hair|"
             r"hair_between_eyes|hair_intakes|sidelocks|hair_over_one_eye|"
             r"hair_bun|braided_bun|half_updo|drill_hair|antenna_hair|gradient_hair|"
             r"two_side_up|front_ponytail|side_braid|low_twintails|short_twintails)(_|$)"),

    ("眼睛", r"(^|_)(eye|eyes|eyelashes|eyebrows|pupils|iris|sclera|"
             r"half-closed_eyes|closed_eyes|one_eye_closed|wide_eyes|empty_eyes|heart-shaped_pupils|"
             r"heterochromia|mismatched_pupils|symbol-shaped_pupils)(_|$)"),

    ("脸部", r"(^|_)(face|facial|blush|light_blush|freckles|mole_(under_eye|on_.*)|"
             r"smile|light_smile|frown|light_frown|grin|open_mouth|closed_mouth|parted_lips|teeth|tongue|"
             r"crying|tears|sad|angry|annoyed|pout|embarrassed|surprised|shocked|scared|"
             r"expression|smug|serious|kissing|licking_lips|biting_lip|yawn|sigh|"
             r"nose)(_|$)"),

    ("身体", r"(^|_)(breasts|nipples|cleavage|collarbone|navel|waist|hips|"
             r"thighs|thigh_gap|legs|arms|crossed_arms|hands|fingers|feet|toes|barefoot|"
             r"buttocks|ass|skin|abs|muscular|slim|tall|short_stature|petite|"
             r"body|figure|silhouette|sweat|sweatdrop|wet_skin|tan|tanlines|"
             r"stomach|midriff|armpits|armpit|fingernails|nail_polish|mole$|flat_chest|"
             r"dark-skinned|dark_skin|pale_skin|abs$)(_|$)"),

    ("非人", r"(^|_)(animal_ears|cat_ears|rabbit_ears|fox_ears|dog_ears|"
             r"tail|cat_tail|fox_tail|multiple_tails|"
             r"horns|halo|wings|feathers|pointy_ears|elf|"
             r"fang|claws|monster_girl|furry|kemonomimi|animal_ear_fluff|ear_fluff|"
             r"demon|angel|slime|scale|dragon|horse_ears|horse_girl|horse_tail|"
             r"pointy_ears)(_|$)"),

    ("上衣", r"(^|_)(shirt|t-shirt|blouse|sweater|cardigan|hoodie|jacket|coat|trench_coat|"
             r"vest|suit|tuxedo|cape|cloak|robe|tank_top|crop_top|tube_top|"
             r"dress|sundress|evening_dress|apron|overalls|kimono_top)(_|$)"),

    ("下装", r"(^|_)(skirt|pleated_skirt|miniskirt|microskirt|pants|jeans|trousers|"
             r"shorts|hot_pants|leggings|bloomers|petticoat|sarong)(_|$)"),

    ("细节", r"(^|_)(long_sleeves|short_sleeves|sleeveless|detached_sleeves|puffy_sleeves|"
             r"bare_shoulders|off_shoulder|open_clothes|unbuttoned|frills|frilled|"
             r"collared|hood|zipper|lace|lace-trimmed|pleated|layered|"
             r"clothing_cutout|backless|high_leg|corset|belt|"
             r"wet_clothes|torn_clothes|alternate_costume|japanese_clothes|"
             r"bowtie|collar$|wide_sleeves|armor|leotard|striped_clothes|"
             r"strapless|sleeves_past_wrists|fur_trim|see-through_clothes|neckerchief|"
             r"thigh_strap|serafuku|"
             r"clothes_lift|clothing_lift|dress_shirt|hairband)(_|$)"),

    ("内衣袜", r"(^|_)(bra|brassiere|panties|underwear|lingerie|garter_belt|"
               r"thighhighs|pantyhose|stockings|socks|kneehighs|legwear|"
               r"zettai_ryouiki|no_panties|no_bra|bottomless)(_|$)"),

    ("鞋配饰", r"(^|_)(hat|cap|beret|helmet|crown|glasses|sunglasses|eyepatch|"
               r"earrings|necklace|choker|bracelet|ring|piercing|"
               r"shoes|boots|heels|sneakers|sandals|loafers|"
               r"ribbon|bow|hairband|hair_ornament|hair_flower|hairclip|headphones|"
               r"gloves|belt|scarf|necktie|ascot|bag|handbag|backpack|purse|"
               r"umbrella|parasol|jewelry|floral_hair)(_|$)"),

    ("制服", r"(^|_)(uniform|school_uniform|sailor|serafuku|gym_uniform|military_uniform|"
             r"maid|maid_apron|nurse|waitress|police|cheerleader|"
             r"kimono|yukata|china_dress|hanfu|cheongsam|"
             r"swimsuit|school_swimsuit|bikini|one-piece_swimsuit|"
             r"gothic|gothic_lolita|lolita_fashion|punk|"
             r"sportswear|casual|formal|western_clothes|traditional_clothes|"
             r"nude_apron|wet_clothes|torn_clothes|unbuttoned)(_|$)"),

    ("动作", r"(^|_)(standing|standing_on_one_leg|sitting|seiza|wariza|lying|on_back|on_stomach|"
             r"on_side|kneeling|squatting|crouching|walking|running|jumping|"
             r"leaning_forward|leaning_back|bending_over|arched_back|stretching|"
             r"arms_up|arms_behind_back|arm_up|hand_up|hands_up|hands_on_hips|"
             r"hand_on_hip|hands_on_own_chest|hand_on_own_face|hand_on_own_cheek|"
             r"crossed_legs|legs_up|leg_up|split|splits|sitting_on_|lying_on_|"
             r"hugging|holding|carrying|reaching_out|pointing|waving|peace_sign|"
             r"salute|stretch|spread_legs|presenting|pose|hand_on_own_hip|hand_on_hip|"
             r"v$|v_sign|peace_sign)(_|$)"),

    ("道具", r"(^|_)(holding_(sword|book|cup|gun|phone|umbrella|flower|food|drink|knife|"
             r"weapon|bag|fan|doll|teddy_bear|cat|animal|plushie)|"
             r"sword|katana|gun|pistol|knife|dagger|spear|bow_(weapon)|"
             r"book|notebook|cup|mug|teacup|bottle|can|fork|spoon|chopsticks|"
             r"phone|cellphone|smartphone|headphones|microphone|guitar|"
             r"bouquet|balloon|gift|box|cigarette|"
             r"food|cake|ice_cream|bread|fruit|strawberry|ramen|coffee|tea|weapon)(_|$)"),

    ("室内", r"(^|_)(indoors|bedroom|on_bed|classroom|kitchen|bathroom|bathtub|shower|"
             r"office|library|bookstore|cafe|restaurant|bar_\(place\)|"
             r"convenience_store|store$|counter$|shopping|"
             r"living_room|sofa|couch|chair|desk|table|window|window_curtain|mirror|"
             r"hallway|stairs|elevator|train_interior|car_interior|"
             r"changing_room|locker_room|gym_\(place\)|hospital|church|castle|ruins)(_|$)"),

    ("室外", r"(^|_)(outdoors|sky|clouds?|tree|trees|forest|grass|field|flower_field|"
             r"street|city|cityscape|building|alley|road|sidewalk|crosswalk|"
             r"beach|sea|ocean|shore|river|lake|waterfall|pool|"
             r"mountain|hill|garden|park|rooftop|balcony|bridge|"
             r"station|train|school_gate|playground|"
             r"amusement_park|festival|market|plant|pot_plant|flower$|water$|"
             r"starry_sky|star_\(symbol\))(_|$)"),

    ("背景", r"(^|_)(simple_background|white_background|black_background|grey_background|"
             r"gradient_background|two-tone_background|checkered_background|"
             r"transparent_background|blurry_background|floral_background|"
             r"star_(field|ry)_sky|scenery|landscape|nature|"
             r"speech_bubble|thought_bubble|heart|:d|twitter_username|bar_censor|censored|"
             r"mosaic_censoring|pelvic_curtain|steam|smoke|fog_\(weather\))(_|$)"),

    ("天气", r"(^|_)(sunny|sunlight|sunset|sunrise|dawn|dusk|golden_hour|blue_hour|"
             r"night|night_sky|starry_sky|moon|moonlight|full_moon|"
             r"rain|rainy|raining|snow|snowing|fog|mist|storm|thunder|"
             r"day|daytime|morning|evening|twilight|cloudy|overcast|"
             r"cherry_blossoms|sakura|autumn_leaves)(_|$)"),

    ("光线", r"(^|_)(lighting|light$|light_rays|lightrays|backlighting|rim_light|cinematic|"
             r"shadow|shadows|cast_shadow|drop_shadow|silhouette|"
             r"glow|glowing|flare|lens_flare|sunbeam|god_rays|"
             r"dark$|darkness|dim|neon|neon_lights|candlelight|firelight|"
             r"moody|atmospheric|hazy|dust|sparkle|sparkles|glitter|"
             r"reflection|reflections|translucent|transparent)(_|$)"),

    ("画风", r"(^|_)(absurdres|highres|lowres|best_quality|masterpiece|"
             r"watercolor|oil_painting|sketch|lineart|monochrome|greyscale|"
             r"flat_color|flat_colors|chibi|realistic|photorealistic|photo-?realistic|"
             r"anime_style|traditional_media|ink|pastel_colors|muted_colors|"
             r"vibrant_colors|colorful|retro|pixel_art|vector|"
             r"depth_of_field|film_grain|artist_name|signature|watermark|"
             r"comic|blurry$|multiple_views|border|dated|english_text|character_name|"
             r"traditional_media|limited_palette)(_|$)"),

    ("涩涩", r"(^|_)(nude|naked|topless|bottomless|undressing|undressed|"
             r"nipples|pussy|vagina|penis|cum|semen|sex|fellatio|cunnilingus|"
             r"masturbation|paizuri|handjob|anal|vaginal|deepthroat|"
             r"pantyshot|upskirt|downblouse|spread_legs|presenting|"
             r"lewd|suggestive|ecchi|erotic|bondage|shibari|"
             r"bra_pull|panty_pull|clothing_pull|breasts_out|areola)(_|$)"),
]

import re

COMPILED = [(name, re.compile(pat)) for name, pat in RULES]

# 二级 → 一级 反查
SUB2PRIMARY = {}
for _p, _subs in PRIMARY:
    for _s in _subs:
        SUB2PRIMARY[_s] = _p


def classify(tag):
    """英文 danbooru tag -> [二级名, ...]（可多归属；一条都不命中 = 未分类）"""
    t = (tag or "").strip().lower().replace(" ", "_")
    return [name for name, rx in COMPILED if rx.search(t)]


def primaries_of(subs):
    """[二级名] -> [一级名]（去重、按 PRIMARY 顺序）"""
    out = []
    for p, lst in PRIMARY:
        if any(s in lst for s in subs) and p not in out:
            out.append(p)
    return out
