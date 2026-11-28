package cn.shouzhuang.sendtocopyq

import android.app.DownloadManager
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.content.res.ColorStateList
import android.net.Uri
import android.os.Build
import android.provider.DocumentsContract
import android.text.format.Formatter
import android.graphics.Typeface
import android.view.Gravity
import android.view.View
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Space
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import com.google.android.material.button.MaterialButton

internal fun incomingFileSheet(context: Context, title: String) = LinearLayout(context).apply {
    orientation = LinearLayout.VERTICAL
    setPadding(context.fileDp(24), context.fileDp(20), context.fileDp(24), context.fileDp(24))
    addView(TextView(context).apply {
        text = title
        textSize = 21f
        gravity = Gravity.CENTER
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        setTextColor(ContextCompat.getColor(context, R.color.primary_text))
    })
}

internal fun receivedFileOffer(
    context: Context,
    offer: IncomingFiles.Offer,
    respond: (Boolean) -> Unit,
) = LinearLayout(context).apply {
    orientation = LinearLayout.VERTICAL
    val info = LinearLayout(context).apply {
        orientation = LinearLayout.VERTICAL
        val total = Formatter.formatFileSize(context, offer.files.sumOf { it.size })
        addView(fileLabel(context, "来自 ${offer.peer.name.ifBlank { "我的电脑" }}"))
        if (offer.files.size > 1) addView(fileLabel(context, "${offer.files.size} 个文件 · $total"))
        offer.files.forEach { addView(fileLabel(context, it.name)) }
        if (offer.files.size == 1) addView(fileLabel(context, total))
    }
    addView(fileBody(context, info))
    addView(fileActions(context, "拒绝", { respond(false) }, "接受", { respond(true) }))
}

internal fun receivingFilePlaceholder(context: Context) = LinearLayout(context).apply {
    orientation = LinearLayout.VERTICAL
    addView(fileBody(context, Space(context)))
    addView(Space(context), LinearLayout.LayoutParams(-1, context.fileDp(68)))
}

internal fun receivedFileResult(
    context: Context,
    files: List<Pair<IncomingFiles.Item, Uri>>,
    fromBackground: Boolean,
    onClose: () -> Unit,
): LinearLayout = LinearLayout(context).apply {
    orientation = LinearLayout.VERTICAL
    val sharedFolder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
    val body = fileBody(context, fileLabel(context,
        if (sharedFolder) "已保存到：下载 / MoryPaste" else "已保存到：MoryPaste 应用内"))
    addView(body)
    val actions = fileActions(context, "关闭", onClose, "打开文件夹") {
        if (sharedFolder) {
            if (openReceivedFolder(context, fromBackground)) onClose()
        } else {
            val list = LinearLayout(context).apply { orientation = LinearLayout.VERTICAL }
            for ((file, uri) in files) {
                list.addView(fileButton(context, file.name, false) {
                    val view = Intent(Intent.ACTION_VIEW).setDataAndType(uri, file.mime)
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    if (launch(context, view, fromBackground)) {
                        if (fromBackground) onClose()
                    } else Toast.makeText(context, "手机上没有可以打开这种文件的应用。", Toast.LENGTH_LONG).show()
                })
            }
            body.removeAllViews()
            body.addView(list)
        }
    }
    addView(actions)
}

private fun Context.fileDp(value: Int) = (value * resources.displayMetrics.density).toInt()

private fun fileLabel(context: Context, value: String) = TextView(context).apply {
    text = value
    textSize = 14f
    setPadding(0, context.fileDp(3), 0, context.fileDp(3))
    setTextColor(ContextCompat.getColor(context, R.color.secondary_text))
}

private fun fileBody(context: Context, body: View) = ScrollView(context).apply {
    val height = (resources.configuration.screenHeightDp / 4).coerceIn(64, 104)
    layoutParams = LinearLayout.LayoutParams(-1, context.fileDp(height)).apply { topMargin = context.fileDp(10) }
    addView(body)
}

private fun fileActions(context: Context, secondary: String, onSecondary: () -> Unit,
                        primary: String, onPrimary: () -> Unit) = LinearLayout(context).apply {
    layoutParams = LinearLayout.LayoutParams(-1, context.fileDp(52)).apply { topMargin = context.fileDp(16) }
    addView(fileButton(context, secondary, false, onSecondary), LinearLayout.LayoutParams(0, -1, 1f))
    addView(fileButton(context, primary, true, onPrimary), LinearLayout.LayoutParams(0, -1, 1f).apply {
        marginStart = context.fileDp(12)
    })
}

private fun fileButton(context: Context, title: String, primary: Boolean, action: () -> Unit) = MaterialButton(context).apply {
    text = title
    insetTop = 0
    insetBottom = 0
    minimumHeight = context.fileDp(52)
    cornerRadius = context.fileDp(14)
    backgroundTintList = ColorStateList.valueOf(ContextCompat.getColor(context,
        if (primary) R.color.brand_blue else R.color.brand_blue_soft))
    setTextColor(ContextCompat.getColor(context, if (primary) R.color.on_brand else R.color.brand_blue))
    setOnClickListener { action() }
}

private fun openReceivedFolder(context: Context, fromBackground: Boolean): Boolean {
    val folder = DocumentsContract.buildDocumentUri(
        "com.android.externalstorage.documents", "primary:${IncomingFiles.DOWNLOAD_PATH}",
    )
    val view = Intent(Intent.ACTION_VIEW).setDataAndType(folder, DocumentsContract.Document.MIME_TYPE_DIR)
    val downloads = Intent(DownloadManager.ACTION_VIEW_DOWNLOADS)
    for (filesApp in listOf("com.android.documentsui", "com.google.android.documentsui")) {
        if (launch(context, Intent(view).setPackage(filesApp), fromBackground)) return true
    }
    val opened = launch(context, downloads, fromBackground)
    val message = if (opened) "请在下载中打开 MoryPaste 文件夹。" else "请在手机文件管理中打开「下载 / MoryPaste」。"
    Toast.makeText(context, message, Toast.LENGTH_LONG).show()
    return opened
}

private fun launch(context: Context, intent: Intent, fromBackground: Boolean): Boolean = try {
    if (fromBackground) intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    context.startActivity(intent)
    true
} catch (_: ActivityNotFoundException) {
    false
} catch (_: SecurityException) {
    false
}
