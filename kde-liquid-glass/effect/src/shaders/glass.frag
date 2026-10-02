#version 140

// Liquid Glass – onscreen pass.
// Vstup je rozmazané pozadí (výsledek dual-kawase z blur efektu KWinu).
// Shader přidává lom na okrajích, barevný rozptyl, lesk na hraně a tón skla.
// Všechny délky jsou v device pixelech relativně k levému hornímu rohu
// zachyceného pozadí (osa y roste dolů, uv.y nahoru).

#include "sdf.glsl"

uniform sampler2D texUnit;
uniform mat4 colorMatrix;
uniform float offset;
uniform vec2 halfpixel;
uniform float opacity;

uniform vec2 texSize;       // velikost zachyceného pozadí v px
uniform vec4 box;           // tvar skla: střed.xy, poloviční rozměr.zw
uniform vec4 cornerRadius;  // zaoblení tvaru skla
uniform float clipEnabled;  // 1 = ořezat do zaobleného tvaru

uniform float edgeWidth;    // šířka pásu u hrany, kde se láme světlo (px)
uniform float refraction;   // o kolik px se pozadí na hraně posune
uniform float chroma;       // barevný rozptyl 0..1
uniform float specular;     // síla lesku 0..1
uniform vec4 tint;          // rgb = barva skla, a = síla
uniform vec2 lightPos;      // poloha kurzoru (px)
uniform float lightOn;      // 1 = lesk sleduje kurzor
uniform vec2 motion;        // setrvačnost při posunu okna (px)
uniform float lightAngle;   // směr pevného světla (rad), animuje se

uniform sampler2D sharpTex; // ostré (nerozmazané) pozadí
uniform vec4 frameBox;      // rám okna (díra v rámečku): střed.xy, poloviční rozměr.zw
uniform vec4 frameRadius;
uniform float hasRing;      // 1 = sklo tvoří i rámeček kolem okna
uniform float ringClarity;  // 0 = rámeček mléčný, 1 = čirý
uniform float time;         // s
uniform float waveAmp;      // amplituda vlnění (px), odeznívá po posunu okna
uniform vec2 waveDir;       // směr posledního pohybu okna

in vec2 uv;
in vec2 vertex;

out vec4 fragColor;

vec4 blurAt(vec2 c)
{
    vec4 sum = texture(texUnit, c + vec2(-halfpixel.x * 2.0, 0.0) * offset);
    sum += texture(texUnit, c + vec2(-halfpixel.x, halfpixel.y) * offset) * 2.0;
    sum += texture(texUnit, c + vec2(0.0, halfpixel.y * 2.0) * offset);
    sum += texture(texUnit, c + vec2(halfpixel.x, halfpixel.y) * offset) * 2.0;
    sum += texture(texUnit, c + vec2(halfpixel.x * 2.0, 0.0) * offset);
    sum += texture(texUnit, c + vec2(halfpixel.x, -halfpixel.y) * offset) * 2.0;
    sum += texture(texUnit, c + vec2(0.0, -halfpixel.y * 2.0) * offset);
    sum += texture(texUnit, c + vec2(-halfpixel.x, -halfpixel.y) * offset) * 2.0;
    return sum / 12.0;
}

float shape(vec2 p)
{
    return sdfRoundedBox(p, box.xy, box.zw, cornerRadius);
}

// posun v px (y dolů) -> posun v uv (v nahoru)
vec2 pxToUv(vec2 px)
{
    return vec2(px.x / texSize.x, -px.y / texSize.y);
}

void main(void)
{
    float d = shape(vertex);           // < 0 uvnitř skla
    float inside = max(-d, 0.0);       // vzdálenost od hrany

    // vnější normála hrany (gradient vzdálenostního pole)
    vec2 g = vec2(shape(vertex + vec2(1.0, 0.0)) - shape(vertex - vec2(1.0, 0.0)),
                  shape(vertex + vec2(0.0, 1.0)) - shape(vertex - vec2(0.0, 1.0)));
    vec2 n = dot(g, g) > 1e-6 ? normalize(g) : vec2(0.0);

    // profil čočky: 1 na hraně, plynule 0 uvnitř
    float e = clamp(1.0 - inside / max(edgeWidth, 1.0), 0.0, 1.0);
    float lens = e * e * (3.0 - 2.0 * e);
    lens *= lens;

    // lom: hrana ukazuje pozadí zpoza okraje, jako by se ohýbalo do skla
    vec2 dispPx = n * refraction * lens;
    // setrvačnost: obsah se při pohybu okna „opozdí“, nejvíc u hran
    dispPx -= motion * (0.3 + 0.7 * lens);

    // vlnění: dvě postupující vlny podél a napříč směru pohybu okna
    if (waveAmp > 0.01) {
        vec2 perp = vec2(-waveDir.y, waveDir.x);
        float along = dot(vertex, waveDir);
        float across = dot(vertex, perp);
        float w1 = sin(along * 0.045 - time * 9.0 + sin(across * 0.02) * 1.5);
        float w2 = sin(across * 0.06 + time * 6.5);
        dispPx += waveDir * w1 * waveAmp + perp * w2 * waveAmp * 0.45;
    }

    // rámeček kolem okna je čirý (ostré pozadí), uvnitř okna mléčné sklo
    float ring = 0.0;
    if (hasRing > 0.5) {
        ring = smoothstep(-1.0, 1.0, sdfRoundedBox(vertex, frameBox.xy, frameBox.zw, frameRadius));
    }
    float clear = ring * ringClarity;

    vec2 base = uv + pxToUv(dispPx);

    vec4 c;
    if (chroma > 0.0 && lens > 0.002) {
        vec2 ca = pxToUv(n * refraction * lens * chroma * 0.6);
        vec4 mid = mix(blurAt(base), texture(sharpTex, base), clear);
        float r = mix(blurAt(base + ca).r, texture(sharpTex, base + ca).r, clear);
        float b = mix(blurAt(base - ca).b, texture(sharpTex, base - ca).b, clear);
        c = vec4(r, mid.g, b, mid.a);
    } else {
        c = mix(blurAt(base), texture(sharpTex, base), clear);
    }
    c = c * colorMatrix;

    // tón skla (světlé „mléčné“ nebo tmavé); čirý rámeček skoro bez tónu
    c.rgb = mix(c.rgb, tint.rgb, tint.a * (1.0 - 0.8 * clear));

    // lesk: tenká linka na hraně + měkký odlesk v pásu čočky
    float rim = 1.0 - smoothstep(0.0, 2.5, inside);
    float fres = e * e * e;
    vec2 L = vec2(cos(lightAngle), sin(lightAngle));
    float key = pow(max(dot(n, L), 0.0), 3.0);
    float back = pow(max(dot(n, -L), 0.0), 3.0) * 0.45;
    float spec = (key + back) * (rim * 0.85 + fres * 0.22) + rim * 0.08;
    if (lightOn > 0.5) {
        float dist = length(vertex - lightPos);
        spec += rim * exp(-dist / 140.0) * 1.1 + fres * exp(-dist / 220.0) * 0.28;
    }
    c.rgb += vec3(specular * spec);

    c.a = 1.0;
    fragColor = c * opacity;

    if (clipEnabled > 0.5) {
        float df = max(fwidth(d), 1e-4);
        fragColor *= 1.0 - clamp(0.5 + d / df, 0.0, 1.0);
    }
}
