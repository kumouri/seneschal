import java.util.Properties
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Endpoints + secrets are injected from local.properties (gitignored) into BuildConfig.
// The empty-string defaults keep a secretless checkout — and CI — building. See the README.
val localProps = Properties().apply {
    val f = rootProject.file("local.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
fun prop(key: String): String = localProps.getProperty(key, "")

android {
    namespace = "com.kumouri.seneschal"
    compileSdk = 36
    buildToolsVersion = "36.1.0"

    defaultConfig {
        applicationId = "com.kumouri.seneschal"
        minSdk = 29
        targetSdk = 36
        versionCode = 1
        versionName = "0.1"

        // Blocklist sync (BlocklistSyncWorker) + health feed (HealthSyncWorker) endpoints.
        buildConfigField("String", "BLOCKLIST_URL", "\"${prop("BLOCKLIST_URL")}\"")
        buildConfigField("String", "BLOCKLIST_SECRET", "\"${prop("BLOCKLIST_SECRET")}\"")
        buildConfigField("String", "HEALTH_INGEST_URL", "\"${prop("HEALTH_INGEST_URL")}\"")
        buildConfigField("String", "HEALTH_INGEST_TOKEN", "\"${prop("HEALTH_INGEST_TOKEN")}\"")
        // Presence feed (geofence/activity/sleep events -> desktop /presence-ingest).
        buildConfigField("String", "PRESENCE_INGEST_URL", "\"${prop("PRESENCE_INGEST_URL")}\"")
        buildConfigField("String", "PRESENCE_INGEST_TOKEN", "\"${prop("PRESENCE_INGEST_TOKEN")}\"")
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        buildConfig = true
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    implementation("androidx.health.connect:connect-client:1.1.0-alpha07")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("com.google.android.gms:play-services-location:21.3.0")
}
