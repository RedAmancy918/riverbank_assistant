import SwiftUI

private let riverCyan = Color(red: 0.337, green: 0.847, blue: 1.0)

struct ContentView: View {
    @EnvironmentObject private var store: AppStore

    var body: some View {
        TabView {
            ChatView()
                .tabItem { Label("Chat", systemImage: "bubble.left.and.bubble.right.fill") }
            VideoCallView()
                .tabItem { Label("通话", systemImage: "video.fill") }
            TaskListView()
                .tabItem { Label("任务", systemImage: "tray.full") }
            ReportListView()
                .tabItem { Label("报告", systemImage: "doc.text") }
            SettingsView()
                .tabItem { Label("设置", systemImage: "gearshape") }
        }
        .tint(riverCyan)
        .overlay(alignment: .top) {
            if !store.errorMessage.isEmpty {
                ErrorBanner(message: store.errorMessage) {
                    withAnimation { store.errorMessage = "" }
                }
                .padding(.top, 4)
                .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
    }
}

struct ErrorBanner: View {
    let message: String
    let dismiss: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill")
            Text(message).font(.footnote).lineLimit(3)
            Spacer(minLength: 4)
            Button(action: dismiss) { Image(systemName: "xmark") }
        }
        .padding(12)
        .foregroundStyle(.white)
        .background(.red.opacity(0.92), in: RoundedRectangle(cornerRadius: 16))
        .padding(.horizontal)
        .shadow(radius: 12)
    }
}

struct StatusBadge: View {
    let task: RemoteTask

    private var color: Color {
        switch task.status {
        case "completed": .green
        case "failed": .red
        case "cancelled": .secondary
        case "waiting_input": .orange
        case "running": riverCyan
        default: .blue
        }
    }

    var body: some View {
        Text(task.statusLabel)
            .font(.caption.weight(.semibold))
            .foregroundStyle(color)
            .padding(.horizontal, 9)
            .padding(.vertical, 5)
            .background(color.opacity(0.14), in: Capsule())
    }
}

extension Date {
    static func taskDate(_ epoch: Double) -> String {
        Date(timeIntervalSince1970: epoch).formatted(
            date: .abbreviated,
            time: .shortened
        )
    }
}
