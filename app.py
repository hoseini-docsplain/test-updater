import customtkinter as ctk

# Read version from README
def get_version():
    try:
        with open('README.md', 'r') as f:
            for line in f:
                if 'Version' in line:
                    return line.strip()
    except:
        return 'Version not found'
    return 'Version not found'

# Create the main window
app = ctk.CTk()
app.geometry('400x300')
app.title('Simple App')

# Create a frame to hold the label (for centering)
frame = ctk.CTkFrame(app)
frame.pack(expand=True)

# Create and pack the label with centered text
label = ctk.CTkLabel(
    frame,
    text=get_version(),
    font=('Arial', 24)
)
label.pack(padx=20, pady=20)

# Run the app
app.mainloop()
