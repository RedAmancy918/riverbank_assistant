import SwiftUI

@main
struct RiverBankMobileApp: App {
    @StateObject private var store = AppStore()

    var body: some Scene {
        WindowGroup {
            RiverBankAppRoot()
                .environmentObject(store)
                .preferredColorScheme(.dark)
        }
    }
}

private struct RiverBankAppRoot: View {
    @State private var showsLaunchScreen = true

    var body: some View {
        ZStack {
            ContentView()

            if showsLaunchScreen {
                RiverBankLaunchView()
                    .transition(.opacity)
                    .zIndex(10)
            }
        }
        .task {
            guard showsLaunchScreen else { return }
            try? await Task.sleep(for: .seconds(2.15))
            withAnimation(.easeInOut(duration: 0.72)) {
                showsLaunchScreen = false
            }
        }
    }
}

private struct RiverBankLaunchView: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var contentOpacity = 0.0

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            VStack(spacing: 34) {
                RiverBankBootMark(isAnimated: !reduceMotion)
                    .frame(width: 132, height: 132)

                VStack(spacing: 17) {
                    Text("RiverBank CO., LTD")
                        .font(.system(size: 31, weight: .regular, design: .serif))
                        .tracking(0.3)
                        .foregroundStyle(Color(white: 0.96))
                        .lineLimit(1)
                        .minimumScaleFactor(0.72)

                    Text("灰 度 流 动")
                        .font(.system(size: 18, weight: .regular, design: .rounded))
                        .tracking(7.5)
                        .foregroundStyle(Color(white: 0.72))
                }
                .padding(.horizontal, 24)
            }
            .offset(y: -14)
            .opacity(contentOpacity)
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
        .onAppear {
            withAnimation(.easeOut(duration: reduceMotion ? 0 : 0.42)) {
                contentOpacity = 1
            }
        }
    }
}

private struct RiverBankBootMark: View {
    let isAnimated: Bool

    private let grayscale = [
        0.85, 0.70, 0.52,
        0.70, 0.52, 0.35,
        0.52, 0.35, 0.14,
    ]

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 60.0, paused: !isAnimated)) { timeline in
            let elapsed = timeline.date.timeIntervalSinceReferenceDate
            Grid(horizontalSpacing: 6, verticalSpacing: 6) {
                ForEach(0..<3, id: \.self) { row in
                    GridRow {
                        ForEach(0..<3, id: \.self) { column in
                            let index = row * 3 + column
                            let wave = isAnimated
                                ? 0.5 + 0.5 * sin(elapsed * 5.4 - Double(index) * 0.58)
                                : 0

                            RoundedRectangle(cornerRadius: 10, style: .continuous)
                                .fill(Color(white: min(1, grayscale[index] + wave * 0.12)))
                                .frame(width: 40, height: 40)
                                .offset(y: isAnimated ? -4.5 * wave : 0)
                                .shadow(
                                    color: .white.opacity(isAnimated ? 0.05 + wave * 0.12 : 0),
                                    radius: 5 + wave * 3
                                )
                        }
                    }
                }
            }
        }
    }
}
