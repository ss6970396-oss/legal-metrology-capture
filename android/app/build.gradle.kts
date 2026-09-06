plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

android {
    namespace = "com.example.legal_metrology_capture"
    compileSdk = flutter.compileSdkVersion
    // No ndkVersion is declared on purpose.
    //
    // Nothing in this app compiles native C/C++. The camera, ML Kit text
    // recognition, barcode scanning and device_info plugins are all
    // Kotlin/Java over prebuilt AARs, and the image analysis is pure Dart.
    // Declaring the template's ndkVersion made AGP require that exact NDK and
    // try to download it, which no part of the build would then have used.
    //
    // See also the path_provider_android pin in pubspec.yaml: from 2.3.0 that
    // plugin drags in package:jni, which does run a native build. Both have to
    // stay as they are for this app to build without an NDK installed.
    // Restore this line if a plugin needing native compilation is ever added.

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        // TODO: Specify your own unique Application ID (https://developer.android.com/studio/build/application-id.html).
        applicationId = "com.example.legal_metrology_capture"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        // Uses the version code from pubspec.yaml. When using split APKs, 1000 * ABI_VERSION
        // is added automatically by Flutter. (https://developer.android.com/studio/build/configure-apk-splits#configure-APK-versions)
        // You can force using the value of versionCode by specifying the `-P force-version-code-ignoring-abi=true`
        // flag during build.
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    buildTypes {
        release {
            // TODO: Add your own signing config for the release build.
            // Signing with the debug keys for now, so `flutter run --release` works.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
}

dependencies {
    // Devanagari text recognition, required for Hindi and Marathi declarations
    // on Indian retail packaging.
    //
    // This has to be declared here, in the app, and not only in the plugin.
    // google_mlkit_text_recognition lists every non-Latin script as
    // `compileOnly`, so the plugin compiles against the Devanagari recognizer
    // but ships no implementation of it. Without this line the Latin path
    // works, `TextRecognitionScript.devanagiri` compiles, and the app then
    // dies with NoClassDefFoundError the first time an inspector points it at
    // a Hindi panel — at runtime, in the field, not at build time here.
    //
    // Keep the version aligned with the plugin's own declaration
    // (`text-recognition:16.0.1`); a mismatch between the base recognizer and
    // a script model is not a supported combination.
    implementation("com.google.mlkit:text-recognition-devanagari:16.0.1")
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}
