#version 140

// Liquid Glass – onscreen pass.
// The input is the blurred background (dual Kawase result from KWin's blur).
// The shader adds refraction at the edges, chromatic dispersion, a specular
// rim and a glass tint. All lengths are in device pixels relative to the
// top-left corner of the captured background (y grows down, uv.y grows up).

#include "sdf.glsl"

uniform sampler2D texUnit;
uniform mat4 colorMatrix;
uniform float offset;
uniform vec2 halfpixel;
uniform float opacity;

uniform vec2 texSize;       // size of the captured background in px
uniform vec4 box;           // glass shape: center.xy, half size.zw
uniform vec4 cornerRadius;  // corner radii of the glass shape
uniform float clipEnabled;  // 1 = clip to the rounded shape

uniform float edgeWidth;    // width of the band along the edge where light refracts (px)
uniform float refraction;   // how many px the background shifts at the edge
uniform float chroma;       // chromatic dispersion 0..1
uniform float specular;     // specular strength 0..1
uniform vec4 tint;          // rgb = glass color, a = strength
uniform vec2 lightPos;      // cursor position (px)
uniform float lightOn;      // 1 = highlight follows the cursor
uniform vec2 motion;        // inertia while a window moves (px)
uniform float lightAngle;   // direction of the fixed light (rad), animated

uniform sampler2D sharpTex; // sharp (unblurred) background
uniform vec4 frameBox;      // window frame (hole in the rim): center.xy, half size.zw
uniform vec4 frameRadius;
uniform float hasRing;      // 1 = the glass includes a rim around the window
uniform float ringClarity;  // 0 = frosted rim, 1 = clear rim
uniform float time;         // s
uniform float waveAmp;      // ripple amplitude (px), fades out after the window stops
uniform vec2 waveDir;       // direction of the last window movement

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

// offset in px (y down) -> offset in uv (v up)
vec2 pxToUv(vec2 px)
{
    return vec2(px.x / texSize.x, -px.y / texSize.y);
}

void main(void)
{
    float d = shape(vertex);           // < 0 inside the glass
    float inside = max(-d, 0.0);       // distance from the edge

    // outward edge normal (gradient of the distance field)
    vec2 g = vec2(shape(vertex + vec2(1.0, 0.0)) - shape(vertex - vec2(1.0, 0.0)),
                  shape(vertex + vec2(0.0, 1.0)) - shape(vertex - vec2(0.0, 1.0)));
    vec2 n = dot(g, g) > 1e-6 ? normalize(g) : vec2(0.0);

    // lens profile: 1 at the edge, smoothly 0 inside
    float e = clamp(1.0 - inside / max(edgeWidth, 1.0), 0.0, 1.0);
    float lens = e * e * (3.0 - 2.0 * e);
    lens *= lens;

    // refraction: the edge shows the background from beyond it, bent into the glass
    vec2 dispPx = n * refraction * lens;
    // inertia: the content "lags behind" while the window moves, most at the edges
    dispPx -= motion * (0.3 + 0.7 * lens);

    // ripples: two travelling waves along and across the window movement
    if (waveAmp > 0.01) {
        vec2 perp = vec2(-waveDir.y, waveDir.x);
        float along = dot(vertex, waveDir);
        float across = dot(vertex, perp);
        float w1 = sin(along * 0.045 - time * 9.0 + sin(across * 0.02) * 1.5);
        float w2 = sin(across * 0.06 + time * 6.5);
        dispPx += waveDir * w1 * waveAmp + perp * w2 * waveAmp * 0.45;
    }

    // the rim around the window is clear (sharp background), the inside is frosted
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

    // glass tint (light "frosted" or dark); the clear rim gets almost no tint
    c.rgb = mix(c.rgb, tint.rgb, tint.a * (1.0 - 0.8 * clear));

    // specular: a thin line on the edge + a soft highlight in the lens band
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
