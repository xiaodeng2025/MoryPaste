import java.security.KeyStore
import java.security.PrivateKey
import java.security.cert.X509Certificate

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val releaseKeystore = providers.environmentVariable("MORYPASTE_ANDROID_KEYSTORE")
val releaseStorePassword = providers.environmentVariable("MORYPASTE_ANDROID_STORE_PASSWORD")
val releaseKeyAlias = providers.environmentVariable("MORYPASTE_ANDROID_KEY_ALIAS")
val releaseKeyPassword = providers.environmentVariable("MORYPASTE_ANDROID_KEY_PASSWORD")

android {
    namespace = "cn.shouzhuang.sendtocopyq"
    compileSdk = 35

    defaultConfig {
        applicationId = "cn.shouzhuang.sendtocopyq"
        minSdk = 26
        targetSdk = 35
        versionCode = 20
        versionName = "0.5.9"
    }

    signingConfigs {
        create("morypasteRelease") {
            storeFile = releaseKeystore.orNull?.takeIf { it.isNotBlank() }?.let { file(it) }
            storeType = "PKCS12"
            storePassword = releaseStorePassword.orNull
            keyAlias = releaseKeyAlias.orNull
            keyPassword = releaseKeyPassword.orNull
        }
    }
    buildTypes.named("release") {
        signingConfig = signingConfigs.getByName("morypasteRelease")
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures {
        buildConfig = true
    }
}

val validateMoryPasteReleaseSigning by tasks.registering {
    doLast {
        fun required(name: String, value: String?): String =
            value?.takeIf { it.isNotBlank() } ?: throw GradleException("Missing Android release signing setting: $name")

        val keystore = file(required("MORYPASTE_ANDROID_KEYSTORE", releaseKeystore.orNull))
        val storePassword = required("MORYPASTE_ANDROID_STORE_PASSWORD", releaseStorePassword.orNull)
        val alias = required("MORYPASTE_ANDROID_KEY_ALIAS", releaseKeyAlias.orNull)
        val keyPassword = required("MORYPASTE_ANDROID_KEY_PASSWORD", releaseKeyPassword.orNull)
        if (!keystore.isFile) throw GradleException("Android release keystore file was not found.")

        val store = KeyStore.getInstance(keystore, storePassword.toCharArray())
        val certificate = store.getCertificate(alias) as? X509Certificate
            ?: throw GradleException("Android release signing alias has no X.509 certificate.")
        if (certificate.subjectX500Principal.name.split(',').any { it.equals("CN=Android Debug", ignoreCase = true) }) {
            throw GradleException("An Android debug certificate cannot sign a public release.")
        }
        certificate.checkValidity()
        if (store.getKey(alias, keyPassword.toCharArray()) !is PrivateKey) {
            throw GradleException("Android release signing alias has no private key.")
        }
    }
}

tasks.matching { it.name == "preReleaseBuild" || it.name == "validateSigningRelease" }.configureEach {
    dependsOn(validateMoryPasteReleaseSigning)
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    implementation("androidx.activity:activity-ktx:1.10.1")
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.camera:camera-camera2:1.4.2")
    implementation("androidx.camera:camera-lifecycle:1.4.2")
    implementation("androidx.camera:camera-view:1.4.2")
    implementation("com.google.zxing:core:3.5.4")
    implementation("com.google.android.material:material:1.12.0")
    testImplementation("junit:junit:4.13.2")
}
