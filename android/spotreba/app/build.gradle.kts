import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Adresa Hanse a klíč pro synchronizaci se do repozitáře NEukládají:
// čtou se z hans.properties vedle projektu (jen na stroji, kde se sestavuje).
val hans = Properties().apply {
    val f = rootProject.file("hans.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
val podpis = Properties().apply {
    val f = rootProject.file("podpis.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}

android {
    namespace = "cz.hans.spotreba"
    compileSdk = 35
    defaultConfig {
        applicationId = "cz.hans.spotreba"
        minSdk = 26
        targetSdk = 35
        versionCode = 9
        versionName = "1.8"
        buildConfigField("String", "HANS_URL", "\"${hans.getProperty("url", "")}\"")
        buildConfigField("String", "HANS_KLIC", "\"${hans.getProperty("klic", "")}\"")
        ndk { abiFilters += listOf("arm64-v8a") }   // současné telefony; menší soubor
    }
    signingConfigs {
        create("vlastni") {
            if (podpis.getProperty("soubor") != null) {
                storeFile = file(podpis.getProperty("soubor"))
                storePassword = podpis.getProperty("heslo")
                keyAlias = podpis.getProperty("alias")
                keyPassword = podpis.getProperty("heslo")
            }
        }
    }
    buildTypes {
        release {
            isMinifyEnabled = false
            if (podpis.getProperty("soubor") != null) signingConfig = signingConfigs.getByName("vlastni")
        }
    }
    buildFeatures { compose = true; buildConfig = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    val compose = platform("androidx.compose:compose-bom:2024.12.01")
    implementation(compose)
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material:material-icons-core")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.work:work-runtime-ktx:2.10.0")
    val camerax = "1.4.1"
    implementation("androidx.camera:camera-camera2:$camerax")
    implementation("androidx.camera:camera-lifecycle:$camerax")
    implementation("androidx.camera:camera-view:$camerax")
    // ML Kit s modely PŘÍMO v aplikaci (offline, bez Google Play služeb)
    implementation("com.google.mlkit:barcode-scanning:17.3.0")
    implementation("com.google.mlkit:text-recognition:16.0.1")
    testImplementation("junit:junit:4.13.2")
}
